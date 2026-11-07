"""How a worker's view is kept apart from the project, chosen once per host.

Every pristine check and every mutant runs in a fresh view of the project at
the worker's fixed path; nothing its command writes reaches the project or the
next view. Two implementations:

- ``NamespaceViews`` (Linux): an overlayfs mount in mutator's private user and
  mount namespace (docs/adr/0006-mutants-run-in-overlay-views.md).
- ``CopyViews`` (Windows): a copy of the project, block-cloned where the
  volume can, with home, profile, temp and app-data redirected beside it
  (docs/adr/0007-windows-workers-run-in-copies.md). Its file copier is a
  parameter, so another host's clone call can plug in.

The engine, scheduling and reporting only see ``level`` and ``describe``.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from mutator.filelock import try_lock, unlock

WINDOWS = sys.platform == "win32"


class IsolationUnavailable(RuntimeError):
    """This host cannot give mutator its isolation; mutator does not run weaker."""

# Build output and the worker tree itself are hidden in a view. ``target``
# would show a worker the directory that contains the worker. Rust workers get
# their own build output instead: a slot target
# (docs/adr/0004-rust-worker-slots-keep-cargo-target.md).
HIDDEN = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        "out",
        "coverage",
        ".metrics",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".clj-kondo",
        ".gradle",
        ".uml-viewer",
        ".toolcache",
    }
)


def hidden_paths(root: Path, relatives, sources) -> set[Path]:
    """Project-relative build output a view shows as an empty directory.

    Those at the project root and beside every directory on the path to a
    source under test or a bound source, when they exist.
    """

    folders = {Path(name).parent for name in [*relatives, *sources]}
    folders |= {parent for folder in list(folders) for parent in folder.parents}
    found = set()
    for folder in folders:
        for name in HIDDEN:
            real = root / folder / name
            if real.exists() or real.is_symlink():
                found.add(folder / name)
    return found


_REPARSE_POINT = 0x400


def is_link(path: Path) -> bool:
    """A symlink, or on Windows any reparse point (a junction too)."""

    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


# Windows refuses to delete a file that a scanner or a just-ended process still
# holds (5: access denied, 32: sharing violation); both pass within seconds.
_DELETE_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0)


def _retrying(action, path: Path) -> None:
    for delay in (*_DELETE_DELAYS, None) if WINDOWS else (None,):
        try:
            action(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if delay is None:
                raise
            try:
                # A read-only file or directory cannot be deleted on Windows.
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            time.sleep(delay)


def delete_tree(path: Path) -> None:
    """Remove worker data, unlinking links without visiting their targets.

    A mounted view is detached first; deleting through it would only fill its
    writable layer.
    """

    try:
        linked = is_link(path)
    except FileNotFoundError:
        return
    if linked:
        # A directory link or junction is removed like an empty directory.
        _retrying(os.rmdir if WINDOWS and path.is_dir() else os.unlink, path)
        return
    if path.is_dir():
        if not WINDOWS and os.path.ismount(path):
            from mutator.sandbox import unmount

            unmount(path)
        if not WINDOWS and not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            # overlayfs leaves its work directory without permissions.
            os.chmod(path, 0o700)
        try:
            children = list(path.iterdir())
        except FileNotFoundError:
            if not WINDOWS:
                raise
            return  # Already removed by the view's own cleanup.
        for child in children:
            delete_tree(child)
        _retrying(os.rmdir, path)
        return
    _retrying(os.unlink, path)


def write_fresh(path: Path, data: bytes) -> None:
    """Write ``data`` and stamp it with the fine-grained current time.

    Cargo treats a source no newer than its last build as unchanged. The
    kernel stamps a write with a clock that can lag the build's own stamp by
    a tick, so the time is set explicitly (docs/adr/0004).
    """

    path.write_bytes(data)
    now = time.time_ns()
    os.utime(path, ns=(now, now))


class NamespaceViews:
    """Overlay views in mutator's private namespace (Linux, ADR 0006).

    Each worker is an empty mount point with a fixed base layer beside it;
    every view mounts the base layer and the project below a fresh, empty
    writable layer.
    """

    level = "namespace-view"

    def __init__(self):
        self._scratch: Path | None = None
        # Views mounted now, so an interrupt can detach them.
        self._mounted: set[Path] = set()

    def describe(self, root: Path) -> str:
        return "namespace views (a private overlayfs view per command)"

    def enter(self) -> None:
        from mutator import sandbox

        sandbox.enter()

    def _layers(self, worker: Path) -> tuple[Path, Path, Path]:
        """The worker's base layer, and the writable and work layers of its view.

        They live outside the project: overlayfs refuses a layer inside another
        layer, and the project is the lowest one.
        """

        if self._scratch is None:
            self._scratch = Path(tempfile.mkdtemp(prefix="mutator-layers-"))
            atexit.register(delete_tree, self._scratch)
        home = self._scratch / hashlib.sha256(str(worker.absolute()).encode()).hexdigest()[:16]
        return home / "base", home / "upper", home / "work"

    def prepare(self, worker: Path, root: Path, relatives, sources: dict[str, bytes]) -> None:
        """The worker's fixed layer: ``sources`` over the project, build output hidden."""

        from mutator.sandbox import make_opaque

        self.remove(worker)
        worker.mkdir(parents=True)
        base = self._layers(worker)[0]
        base.mkdir(parents=True)
        for hidden in hidden_paths(root, relatives, sources):
            (base / hidden).mkdir(parents=True, exist_ok=True)
            make_opaque(base / hidden)
        for name, data in sources.items():
            (base / name).parent.mkdir(parents=True, exist_ok=True)
            (base / name).write_bytes(data)

    @contextmanager
    def view(self, worker: Path, root: Path, relative: str, data: bytes):
        """A fresh view at ``worker`` holding ``data`` at ``relative``.

        The source is stamped with the current time even when it is the
        original: a slot target may hold a newer build of a mutant
        (docs/adr/0004).
        """

        from mutator.sandbox import mount_overlay, unmount

        base, upper, work = self._layers(worker)
        for layer in (upper, work):
            delete_tree(layer)
            layer.mkdir()
        mount_overlay(worker, [base, root.resolve()], upper, work)
        self._mounted.add(worker)
        try:
            write_fresh(worker / relative, data)
            yield
        finally:
            unmount(worker)
            self._mounted.discard(worker)
            delete_tree(upper)
            delete_tree(work)

    def remove(self, worker: Path) -> None:
        delete_tree(worker)
        delete_tree(self._layers(worker)[0].parent)

    def discard(self) -> None:
        """Detach every mounted view."""

        from mutator.sandbox import unmount

        for worker in list(self._mounted):
            unmount(worker)
            self._mounted.discard(worker)

    def redirect(self, worker: Path, environment: dict) -> None:
        """Nothing to redirect: a view's writes stay in its writable layer."""

    def claim_run(self, directory: Path) -> None:
        """Views vanish with the process; a run directory needs no owner."""

    def release_run(self, directory: Path) -> None:
        pass

    def sweep(self, workers: Path) -> None:
        pass


# Windows 11 24H2: CopyFile2 block-clones by itself on a ReFS (Dev Drive) volume.
BLOCK_CLONE_BUILD = 26100
_FILE_SUPPORTS_BLOCK_REFCOUNTING = 0x08000000


def copy_method(filesystem: str, flags: int, build: int | None) -> str:
    """"block clone" when the volume and build clone copies, else "full copy"."""

    if (filesystem.upper() == "REFS" and flags & _FILE_SUPPORTS_BLOCK_REFCOUNTING
            and build is not None and build >= BLOCK_CLONE_BUILD):
        return "block clone"
    return "full copy"


def volume(path: Path) -> tuple[str, int]:
    """The filesystem name and flags of the volume holding ``path`` (Windows)."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mount = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetVolumePathNameW(str(path), mount, len(mount)):
        return "", 0
    name = ctypes.create_unicode_buffer(64)
    flags = wintypes.DWORD()
    if not kernel32.GetVolumeInformationW(mount.value, None, 0, None, None, ctypes.byref(flags),
                                          name, len(name)):
        return "", 0
    return name.value, flags.value


# Created fresh for every view, beside the worker, never inside the copy.
_REDIRECTED = {
    "HOME": "home",
    "USERPROFILE": "home",
    "TEMP": "temp",
    "TMP": "temp",
    "APPDATA": "appdata/Roaming",
    "LOCALAPPDATA": "appdata/Local",
}


def _toolchain_homes(environment: dict) -> dict[str, str]:
    """Where toolchains keep their installs and caches, before home moves.

    Without these, cargo (a rustup proxy under the profile), go, a user-site
    Python package or npm would look for themselves in the empty home.
    """

    profile = environment.get("USERPROFILE", "")
    roaming = environment.get("APPDATA", "")
    local = environment.get("LOCALAPPDATA", "")
    gopath = environment.get("GOPATH") or os.path.join(profile, "go")
    defaults = {
        "CARGO_HOME": os.path.join(profile, ".cargo"),
        "RUSTUP_HOME": os.path.join(profile, ".rustup"),
        "GOPATH": gopath,
        "GOMODCACHE": os.path.join(gopath, "pkg", "mod"),
        "GOCACHE": os.path.join(local, "go-build"),
        "PYTHONUSERBASE": os.path.join(roaming, "Python"),
        "npm_config_cache": os.path.join(local, "npm-cache"),
        "npm_config_prefix": os.path.join(roaming, "npm"),
    }
    return {
        name: value for name, value in defaults.items()
        if name not in environment and os.path.isdir(value)
    }


def _home_of(worker: Path) -> Path:
    """The redirected home of a view: beside the worker, outside its tree."""

    return worker.parent / f".home-{worker.name}"


def _key(worker: Path) -> str:
    return os.path.normcase(os.path.realpath(worker))


def _copy_tree(source: Path, destination: Path, skip: set[Path], copy_file) -> None:
    """Copy ``source`` to ``destination`` the way a view shows it.

    ``skip`` (hidden build output) becomes an empty directory. Links are
    recreated, never followed. ``node_modules`` is linked to the project's
    installed copy, as a virtualenv is used in place.
    """

    destination.mkdir()
    with os.scandir(source) as entries:
        for entry in entries:
            path, target = Path(entry.path), destination / entry.name
            if path in skip:
                target.mkdir()
            elif entry.is_symlink() or _is_junction(path):
                _relink(path, target)
            elif entry.is_dir():
                if entry.name == "node_modules":
                    _link_directory(Path(os.path.realpath(path)), target)
                else:
                    _copy_tree(path, target, skip, copy_file)
            elif entry.is_file():
                copy_file(path, target)


def _is_junction(path: Path) -> bool:
    # Path.is_junction is Python 3.12+; there are no junctions off Windows.
    return getattr(path, "is_junction", lambda: False)()


def _link_directory(real: Path, target: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(real), str(target))


def _relink(path: Path, target: Path) -> None:
    """Recreate a project link; a junction where symlinks need a privilege."""

    if _is_junction(path):
        _link_directory(Path(os.path.realpath(path)), target)
        return
    pointed = os.readlink(path)
    directory = path.is_dir()
    try:
        os.symlink(pointed, target, target_is_directory=directory)
    except OSError as error:
        if getattr(error, "winerror", None) != 1314:  # no symlink privilege
            raise
        real = Path(os.path.realpath(path))
        if directory:
            _link_directory(real, target)
        elif real.is_file():
            shutil.copy2(real, target)


class CopyViews:
    """A fresh copy of the project per command (Windows, ADR 0007).

    The copy is made at the worker's fixed path, on the project's volume, with
    the overlay views' exclusions, and deleted after the command. Home,
    profile, temp and app-data point at a fresh directory beside it.
    """

    level = "copy"

    def __init__(self, copy_file=shutil.copy2):
        # copy2 is CopyFile2 on Windows: it keeps mtimes (Cargo fingerprints)
        # and block-clones by itself on a capable volume.
        self.copy_file = copy_file
        self._plans: dict[str, tuple[Path, set[Path], dict[str, bytes]]] = {}
        self._homes: dict[str, Path] = {}
        self._active: set[Path] = set()
        self._runs: dict[Path, int] = {}

    def describe(self, root: Path) -> str:
        filesystem, flags = volume(root) if WINDOWS else ("", 0)
        build = sys.getwindowsversion().build if WINDOWS else None
        method = copy_method(filesystem, flags, build)
        on = f" on {filesystem}" if filesystem else ""
        return f"copy (a fresh {method} of the project per command{on})"

    def enter(self) -> None:
        """Copies need nothing from the system up front."""

    def prepare(self, worker: Path, root: Path, relatives, sources: dict[str, bytes]) -> None:
        self.remove(worker)
        # An empty worker between views, as a mount point is on Linux.
        worker.mkdir(parents=True)
        self._plans[_key(worker)] = (root.resolve(), hidden_paths(root, relatives, sources), dict(sources))

    @contextmanager
    def view(self, worker: Path, root: Path, relative: str, data: bytes):
        """A fresh copy at ``worker`` holding ``data`` at ``relative``."""

        key = _key(worker)
        project, hidden, sources = self._plans.get(key) or (root.resolve(), hidden_paths(root, [relative], {}), {})
        home = _home_of(worker)
        self._active.add(worker)
        try:
            delete_tree(worker)
            delete_tree(home)
            _copy_tree(project, worker, {project / path for path in hidden}, self.copy_file)
            for name, content in sources.items():
                (worker / name).parent.mkdir(parents=True, exist_ok=True)
                (worker / name).write_bytes(content)
            write_fresh(worker / relative, data)
            for folder in set(_REDIRECTED.values()):
                (home / folder).mkdir(parents=True, exist_ok=True)
            self._homes[key] = home
            yield
        finally:
            self._homes.pop(key, None)
            delete_tree(worker)
            delete_tree(home)
            self._active.discard(worker)
            # Empty again between views, like a mount point after unmount.
            worker.mkdir(parents=True, exist_ok=True)

    def redirect(self, worker: Path, environment: dict) -> None:
        """Point home, profile, temp and app-data of a view's command beside it."""

        home = self._homes.get(_key(worker))
        if home is None:
            return
        environment.update(_toolchain_homes(environment))
        for name, folder in _REDIRECTED.items():
            environment[name] = str(home / folder)

    def remove(self, worker: Path) -> None:
        self._plans.pop(_key(worker), None)
        delete_tree(worker)
        delete_tree(_home_of(worker))

    def discard(self) -> None:
        """Delete the copies of views still open; their commands have ended."""

        for worker in list(self._active):
            delete_tree(worker)
            delete_tree(_home_of(worker))
            self._active.discard(worker)

    # A killed mutator leaves its copies on disk, unlike a namespace's views.
    # Each run directory holds a lock for as long as its process lives, so the
    # next run can tell an abandoned one from a running one.

    def claim_run(self, directory: Path) -> None:
        descriptor = os.open(directory / "lock", os.O_RDWR | os.O_CREAT, 0o644)
        if not try_lock(descriptor):
            os.close(descriptor)
            raise OSError(f"cannot lock new run directory {directory}")
        self._runs[directory] = descriptor

    def release_run(self, directory: Path) -> None:
        descriptor = self._runs.pop(directory, None)
        if descriptor is not None:
            unlock(descriptor)
            os.close(descriptor)

    def sweep(self, workers: Path) -> None:
        """Delete run directories whose process is gone."""

        if not workers.is_dir():
            return
        for directory in workers.glob("run-*"):
            if directory in self._runs:
                continue
            lock = directory / "lock"
            if not lock.exists():
                # Being created right now, or left by a run from before locks.
                if time.time() - directory.stat().st_mtime < 60:
                    continue
            else:
                try:
                    descriptor = os.open(lock, os.O_RDWR)
                except OSError:
                    continue
                held = not try_lock(descriptor)
                if not held:
                    unlock(descriptor)
                os.close(descriptor)
                if held:
                    continue
            try:
                delete_tree(directory)
            except OSError as error:
                print(f"Could not remove the abandoned {directory}: {error}", file=sys.stderr)


def for_platform(platform: str):
    """Copies on native Windows; namespace views everywhere else."""

    return CopyViews() if platform == "win32" else NamespaceViews()


_current = None


def current():
    """The isolation of this host, chosen once."""

    global _current
    if _current is None:
        _current = for_platform(sys.platform)
    return _current
