"""Run mutants in overlay views.

Each worker is an empty directory under target/mutation-workers with a fixed
base layer beside it. Every command runs in a fresh overlay view mounted at
the worker: the base layer and the project tree below, an empty writable
layer on top. The source under test is written into the view, and the view is
discarded afterwards, so nothing a test writes reaches the project or the next
mutant. A Lua run keeps its workers across files; Rust runs in worker slots
whose paths and Cargo targets persist across files and runs; other callers use
per-file workers. (docs/adr/0006-mutants-run-in-overlay-views.md)
"""

from __future__ import annotations

import atexit
import fcntl
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock

from mutator.model import Site
from mutator.runner import SLOT_PREFIX, Command, run_tests
from mutator.sandbox import make_opaque, mount_overlay, unmount
from mutator.sites import apply_site
from mutator.timings import measure, record

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


def worker_count(site_count: int, requested: int | None) -> int:
    """The smaller of the sites, the cores, and an explicit --max-workers."""

    processors = os.cpu_count() or 1
    limit = processors if requested is None else requested
    return max(1, min(site_count, processors, limit))


# Every run directory this process made, so an interrupt can remove leftovers.
_created_runs: set[Path] = set()


def new_run_dir(root: Path) -> Path:
    directory = root / "target" / "mutation-workers" / f"run-{uuid.uuid4()}"
    directory.mkdir(parents=True)
    _created_runs.add(directory)
    return directory


def discard_overlays() -> None:
    """Remove the overlays this process created and has not removed yet.

    Worker slots lose their overlay and keep their slot target.
    """

    for worker in list(_mounted):
        unmount(worker)
        _mounted.discard(worker)
    for directory in list(_created_runs):
        _delete_run(directory)
        _created_runs.discard(directory)
    for slot in list(_held_slots):
        _remove_worker(slot / "worker")


def _is_link(path: Path) -> bool:
    return stat.S_ISLNK(path.lstat().st_mode)


def delete_tree(path: Path) -> None:
    """Remove worker data, unlinking links without visiting their targets.

    A mounted view is detached first; deleting through it would only fill its
    writable layer.
    """

    try:
        linked = _is_link(path)
    except FileNotFoundError:
        return
    if linked:
        path.unlink()
        return
    if path.is_dir():
        if os.path.ismount(path):
            unmount(path)
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            # overlayfs leaves its work directory without permissions.
            os.chmod(path, 0o700)
        for child in list(path.iterdir()):
            delete_tree(child)
        path.rmdir()
        return
    path.unlink()


_scratch: Path | None = None


def _layers(worker: Path) -> tuple[Path, Path, Path]:
    """The worker's base layer, and the writable and work layers of its view.

    They live outside the project: overlayfs refuses a layer inside another
    layer, and the project is the lowest one.
    """

    global _scratch
    if _scratch is None:
        _scratch = Path(tempfile.mkdtemp(prefix="mutator-layers-"))
        atexit.register(delete_tree, _scratch)
    home = _scratch / hashlib.sha256(str(worker.absolute()).encode()).hexdigest()[:16]
    return home / "base", home / "upper", home / "work"


def _delete_run(directory: Path) -> None:
    for worker in directory.glob("worker-*"):
        _remove_worker(worker)
    delete_tree(directory)


def _remove_worker(worker: Path) -> None:
    delete_tree(worker)
    delete_tree(_layers(worker)[0].parent)


def _build_base(worker: Path, root: Path, relatives, sources: dict[str, bytes]) -> None:
    """The worker's fixed layer: ``sources`` over the project, build output hidden.

    Hidden directories are those at the project root and beside every
    directory on the path to a source under test.
    """

    _remove_worker(worker)
    worker.mkdir(parents=True)
    base = _layers(worker)[0]
    base.mkdir(parents=True)
    folders = {Path(name).parent for name in [*relatives, *sources]}
    folders |= {parent for folder in list(folders) for parent in folder.parents}
    for folder in folders:
        for name in HIDDEN:
            real = root / folder / name
            if real.exists() or real.is_symlink():
                hidden = base / folder / name
                hidden.mkdir(parents=True, exist_ok=True)
                make_opaque(hidden)
    for name, data in sources.items():
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_bytes(data)


# Views mounted now, so an interrupt can detach them.
_mounted: set[Path] = set()


@contextmanager
def _view(worker: Path, root: Path, relative: str, data: bytes):
    """A fresh view at ``worker`` holding ``data`` at ``relative``.

    The source is stamped with the current time even when it is the
    original: a slot target may hold a newer build of a mutant
    (docs/adr/0004).
    """

    base, upper, work = _layers(worker)
    for layer in (upper, work):
        delete_tree(layer)
        layer.mkdir()
    mount_overlay(worker, [base, root.resolve()], upper, work)
    _mounted.add(worker)
    try:
        _write_fresh(worker / relative, data)
        yield
    finally:
        unmount(worker)
        _mounted.discard(worker)
        delete_tree(upper)
        delete_tree(work)


def _run_in_view(directory, root, relative, data, runner, command, cwd, timeout):
    with _view(directory, root, relative, data):
        return run_tests(runner, command, mapped_cwd(directory, root, cwd), timeout)


def create_workers(
    base: Path, root: Path, relative: str, original: bytes, count: int,
    *, start: int = 0, sources: dict[str, bytes] | None = None,
) -> list[Path]:
    """Workers ``base/worker-<n>`` for ``relative``; ``sources`` go in their base layer."""

    created = []
    try:
        for index in range(start, start + count):
            worker = base / f"worker-{index}"
            _build_base(worker, root, [relative], sources or {})
            created.append(worker)
    except BaseException:
        _delete_run(base)
        raise
    return created


def _write_fresh(path: Path, data: bytes) -> None:
    """Write ``data`` and stamp it with the fine-grained current time.

    Cargo treats a source no newer than its last build as unchanged. The
    kernel stamps a write with a clock that can lag the build's own stamp by
    a tick, so the time is set explicitly (docs/adr/0004).
    """

    path.write_bytes(data)
    now = time.time_ns()
    os.utime(path, ns=(now, now))


def mapped_cwd(worker: Path, root: Path, cwd: Path) -> Path:
    try:
        relative = cwd.resolve().relative_to(root.resolve())
    except ValueError:
        return worker
    return (worker / relative).resolve()


def _execute(
    directory: Path,
    site: Site,
    root: Path,
    relative: str,
    original: bytes,
    runner,
    command: Command,
    cwd: Path,
    timeout: float,
    file_key: str,
    plans: dict[str, tuple[Command, float]] | None = None,
    phase: str = "mutant",
) -> str:
    if plans is not None:
        command, timeout = plans.get(site.mutation_id, (command, timeout))
    current = original[site.start : site.end].decode("utf-8")
    if current != site.original:
        print(
            f"Skipped {file_key}:{site.line} {site.description}; source bytes moved",
            file=sys.stderr,
        )
        return "survived"
    mutated = apply_site(original.decode("utf-8"), site.start, site.end, site.mutant)
    result = _run_in_view(directory, root, relative, mutated.encode("utf-8"),
                          runner, command, cwd, timeout)
    record(phase, getattr(result, "seconds", 0.0))
    if runner.verbose:
        print(
            f"result {phase} {file_key}:{site.line} {site.description}: "
            f"{getattr(result, 'seconds', 0.0):.3f}s, "
            f"exit={result.code}, timed_out={result.timed_out}",
            file=sys.stderr,
        )
    if result.launch_error is not None:
        raise result.launch_error
    if result.timed_out or result.code != 0:
        return "killed"
    return "survived"


def _drain(
    directory: Path,
    pending: Queue,
    lock: Lock,
    outcomes: dict[str, str],
    root: Path,
    relative: str,
    original: bytes,
    runner,
    command: Command,
    cwd: Path,
    timeout: float,
    file_key: str,
    plans: dict[str, tuple[Command, float]] | None,
    checked_command: Command,
    first_site: Site | None = None,
    shared_checks: set | None = None,
) -> None:
    checked = {tuple(checked_command) if isinstance(checked_command, list) else checked_command}
    while True:
        if first_site is not None:
            site, first_site = first_site, None
        else:
            try:
                site = pending.get_nowait()
            except Empty:
                return
        if plans is not None:
            selected_command, selected_timeout = plans.get(site.mutation_id, (command, timeout))
            key = tuple(selected_command) if isinstance(selected_command, list) else selected_command
            if key not in checked:
                if shared_checks is None:
                    _check_worker(directory, root, relative, original, cwd, runner,
                                  selected_command, selected_timeout)
                else:
                    # One pristine run per command across the run; every
                    # other worker's view is built the same way.
                    with lock:
                        first = key not in shared_checks
                        if first:
                            shared_checks.add(key)
                    if first:
                        _check_worker(directory, root, relative, original, cwd, runner,
                                      selected_command, selected_timeout)
                checked.add(key)
        if runner.verbose:
            with lock:
                _report(file_key, site)
        status = _execute(
            directory,
            site,
            root,
            relative,
            original,
            runner,
            command,
            cwd,
            timeout,
            file_key,
            plans,
        )
        with lock:
            outcomes[site.mutation_id] = status


def _report(file_key: str, site: Site) -> None:
    print(f"{file_key}:{site.line} {site.description}", file=sys.stderr)


def _run_all(
    directories: list[Path],
    sites: list[Site],
    outcomes: dict[str, str],
    root: Path,
    relative: str,
    original: bytes,
    runner,
    command: Command,
    cwd: Path,
    timeout: float,
    file_key: str,
    plans: dict[str, tuple[Command, float]] | None,
    checked_command: Command,
    reserved: list[Site] | None = None,
    shared_checks: set | None = None,
) -> None:
    pending: Queue = Queue()
    for site in sites[len(reserved or []):]:
        pending.put(site)
    lock = Lock()
    with ThreadPoolExecutor(max_workers=len(directories)) as pool:
        futures = [
            pool.submit(
                _drain,
                directory,
                pending,
                lock,
                outcomes,
                root,
                relative,
                original,
                runner,
                command,
                cwd,
                timeout,
                file_key,
                plans,
                plans.get(reserved[index].mutation_id, (command, timeout))[0]
                if reserved is not None and plans is not None else checked_command,
                reserved[index] if reserved is not None else None,
                shared_checks,
            )
            for index, directory in enumerate(directories)
        ]
        for future in futures:
            future.result()


class WorkerBaselineError(RuntimeError):
    """An unmutated overlay cannot execute the project's test command."""


def _check_worker(directory, root, relative, original, cwd, runner, command, timeout) -> None:
    with measure("worker-check"):
        result = _run_in_view(directory, root, relative, original, runner, command, cwd, timeout)
    if result.launch_error is not None:
        raise result.launch_error
    if result.timed_out or result.code != 0:
        tail = "\n".join(result.output.splitlines()[-20:])
        # The run directory is deleted afterwards; a 20-line tail rarely names
        # the failing test, so keep the whole output next to it.
        log = root / "target" / "mutation-logs" / f"{directory.parent.name}-{directory.name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(result.output, encoding="utf-8")
        raise WorkerBaselineError(
            f"Unmutated worker baseline failed in {directory} "
            f"(exit {result.code}, timed_out={result.timed_out}); full output: {log}\n{tail}"
        )


def _project_state(root: Path) -> dict[str, tuple[str, bytes | str]]:
    """Fingerprint the project's data and link bindings without following directory links."""
    state = {}

    def visit(folder):
        for path in folder.iterdir():
            if path.name in HIDDEN or path.name in {"node_modules", ".claude", ".agents"}:
                continue
            key = path.relative_to(root).as_posix()
            if _is_link(path):
                state[key] = ("link", str(path.resolve()))
                # A link out of the project is an external input, like an
                # installed library: its binding is frozen, its data is not.
                # A link back into the project leads a view to the real tree,
                # past the private sources, so it stays rejected.
                if path.is_dir() and path.resolve().is_relative_to(root.resolve()):
                    raise WorkerBaselineError(f"Worker reuse cannot freeze linked project directory: {path}")
                if path.is_file():
                    state[key + "/@contents"] = ("file", hashlib.sha256(path.read_bytes()).digest())
            elif path.is_dir():
                state[key] = ("directory", "")
                visit(path)
            else:
                state[key] = ("file", hashlib.sha256(path.read_bytes()).digest())

    visit(root)
    return state


class WorkerPool:
    """Run-scoped Lua workers with a fixed set of private source bindings.

    Every worker's base layer holds the bytes each source had when the pool
    was made. A change to the real tree's other inputs poisons the pool;
    callers must discard it rather than trust a kill. Commands are still
    checked separately on every working directory.
    """

    def __init__(self, root: Path, sources: list[Path], check_each_worker: bool = True):
        self.root = root.resolve()
        self.sources = {
            path.resolve().relative_to(self.root).as_posix(): path.read_bytes()
            for path in sources
        }
        # False: one pristine command run per run, not per worker. Every
        # worker's view is built from the same layers.
        self.check_each_worker = check_each_worker
        self.base: Path | None = None
        self.directories: list[Path] = []
        self.project_state = None
        self.failed = False
        self.checked: set = set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.base is not None:
            _delete_run(self.base)
        self.failed = True

    def prepare(self, relative, original, count):
        if self.failed or self.sources.get(relative) != original:
            raise WorkerBaselineError("Worker pool source binding changed or pool failed")
        if self.base is None:
            self.base = new_run_dir(self.root)
            try:
                self.project_state = self._project_state()
            except BaseException:
                self.close()
                raise
        self.validate()
        if len(self.directories) < count:
            self.directories.extend(create_workers(
                self.base, self.root, relative, original,
                count - len(self.directories), start=len(self.directories),
                sources=self.sources))
        return self.directories[:count]

    def _project_state(self):
        """The real tree's fingerprint, without the bound sources.

        Every worker holds its own copy of them, so an edit to one in the real
        tree cannot reach another file's mutants. The engine checks each
        source itself before publishing it.
        """
        state = _project_state(self.root)
        for name in self.sources:
            state.pop(name, None)
            state.pop(name + "/@contents", None)
        return state

    def validate(self):
        """The real tree only. Safe while workers hold mutants of other files."""
        if self._project_state() != self.project_state:
            self.failed = True
            raise WorkerBaselineError("Project inputs changed during worker reuse")

    def check(self, directory, root, relative, original, cwd, runner, command, timeout):
        _check_once(self.checked, directory, root, relative, original, cwd, runner, command, timeout)


def _check_once(checked: set, directory, root, relative, original, cwd, runner, command, timeout) -> None:
    # Every pristine view is the same whichever bound file it writes.
    key = (directory, str(cwd),
           ("shell", command) if isinstance(command, str) else ("argv", *command))
    if key not in checked:
        _check_worker(directory, root, relative, original, cwd, runner, command, timeout)
        checked.add(key)


# Slots this process holds, so an interrupt can remove their overlays.
_held_slots: set[Path] = set()


def _seed(root: Path, target: Path) -> None:
    """Start a new slot target from the project's own debug build, if any.

    Copied with mtimes so Cargo's fingerprints stay fresh; a reflink makes
    the copy cheap where the filesystem allows. Without one the slot builds
    cold once.
    """

    source = root / "target" / "debug"
    if target.exists() or not source.is_dir():
        return
    partial = target.with_name(target.name + ".partial")
    delete_tree(partial)
    partial.mkdir(parents=True)
    copied = subprocess.run(
        ["cp", "-a", "--reflink=auto", str(source), str(partial / "debug")],
        capture_output=True, text=True,
    )
    if copied.returncode != 0:
        print(f"Could not seed {target} from {source}; building cold.\n{copied.stderr}",
              file=sys.stderr)
        delete_tree(partial)
        return
    partial.rename(target)


def _package_name(root: Path, relative: str) -> str | None:
    """The Cargo package that owns ``relative``: the nearest manifest with one."""

    directory = (root / relative).parent
    while True:
        manifest = directory / "Cargo.toml"
        if manifest.is_file():
            try:
                name = tomllib.loads(manifest.read_text(encoding="utf-8")).get("package", {}).get("name")
            except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                name = None
            if isinstance(name, str):
                return name
        if directory == root or directory.parent == directory:
            return None
        directory = directory.parent


def _invalidate(root: Path, target: Path, relative: str) -> None:
    """Make Cargo rebuild the package of a source a previous file mutated.

    Its last build in this slot may be of a mutant, and the overlay now links
    the original, whose mtime is older than that build. Removing the
    package's fingerprints is what makes Cargo rebuild it.
    """

    if not target.is_dir():
        return
    name = _package_name(root, relative)
    entries = [entry for fingerprints in [*target.glob("*/.fingerprint"), *target.glob("*/*/.fingerprint")]
               for entry in fingerprints.iterdir()]
    pattern = re.compile(re.escape(name or "") + r"-[0-9a-f]{16}")
    matched = [entry for entry in entries if name is not None and pattern.fullmatch(entry.name)]
    if entries and not matched:
        # An unrecognised package or fingerprint layout: a cold build is
        # slower, a reused mutant build is wrong.
        print(f"Cannot find the Cargo fingerprints of {relative} in {target}; "
              "discarding the slot target.", file=sys.stderr)
        delete_tree(target)
        return
    for entry in matched:
        shutil.rmtree(entry)


class WorkerSlots:
    """Rust worker slots: fixed worker paths whose Cargo targets persist.

    Slot n is ``target/mutation-workers/cargo-slot-<n>``: ``worker`` is where
    every view of the slot is mounted, at the same path for every file;
    ``worker.base`` is its base layer, rebuilt for every file; ``target`` is
    the slot target that every command in the slot builds into; ``lock`` is
    held for the run, so a concurrent mutator takes another number;
    ``mutated`` names the sources whose package may hold a mutant build.
    (docs/adr/0004-rust-worker-slots-keep-cargo-target.md)
    """

    def __init__(self, root: Path, check_each_worker: bool = True):
        self.root = root.resolve()
        self.check_each_worker = check_each_worker
        self.slots: list[Path] = []
        self.locks: list[int] = []
        self.checked: set = set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()

    def _acquire(self) -> None:
        base = self.root / "target" / "mutation-workers"
        number = 0
        while True:
            slot = base / f"{SLOT_PREFIX}{number}"
            number += 1
            if slot in self.slots:
                continue
            slot.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(slot / "lock", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            self.slots.append(slot)
            self.locks.append(descriptor)
            _held_slots.add(slot)
            return

    def prepare(self, relative, original, count):
        while len(self.slots) < count:
            self._acquire()
        directories = []
        try:
            for index, slot in enumerate(self.slots):
                worker = slot / "worker"
                _remove_worker(worker)
                if index >= count:
                    continue
                _seed(self.root, slot / "target")
                marker = slot / "mutated"
                if marker.is_file():
                    for previous in marker.read_text(encoding="utf-8").splitlines():
                        _invalidate(self.root, slot / "target", previous)
                # Written before any mutant, so an interrupted run is undone too.
                marker.write_text(relative + "\n", encoding="utf-8")
                _build_base(worker, self.root, [relative], {})
                directories.append(worker)
        except BaseException:
            self.close()
            raise
        return directories

    def validate(self):
        """Nothing to compare: every command runs in a fresh view."""

    def check(self, directory, root, relative, original, cwd, runner, command, timeout):
        _check_once(self.checked, directory, root, relative, original, cwd, runner, command, timeout)

    def close(self):
        """Remove the workers. Slot targets and locks stay."""

        for slot in self.slots:
            _remove_worker(slot / "worker")

    def release(self):
        self.close()
        for slot, descriptor in zip(self.slots, self.locks):
            _held_slots.discard(slot)
            os.close(descriptor)
        self.slots, self.locks = [], []


def run_mutants(
    root: Path,
    source: Path,
    original: bytes,
    sites: list[Site],
    max_workers: int | None,
    runner,
    command: Command,
    cwd: Path,
    timeout: float,
    file_key: str,
    outcomes: dict[str, str],
    *,
    plans: dict[str, tuple[Command, float]] | None = None,
    check_command: Command | None = None,
    check_timeout: float | None = None,
    confirmation: tuple[Command, float] | None = None,
    pool: WorkerPool | None = None,
) -> None:
    """Mutate ``sites`` in parallel overlays of ``root``. The source stays put."""

    if not sites:
        return
    if pool is not None and root.resolve() != pool.root:
        pool.close()
        raise WorkerBaselineError("Worker pool belongs to a different project root")
    base = new_run_dir(root) if pool is None else None
    try:
        relative = source.resolve().relative_to(root.resolve()).as_posix()
        with measure("worker-prepare"):
            count = worker_count(len(sites), max_workers)
            directories = (
                create_workers(base, root, relative, original, count)
                if pool is None else pool.prepare(relative, original, count)
            )
        runner.workers = len(directories)
        reserved = sites[:len(directories)] if plans is not None else None
        first_plans = [
            plans.get(site.mutation_id, (command, timeout)) for site in reserved
        ] if reserved is not None else [
            (command if check_command is None else check_command,
             timeout if check_timeout is None else check_timeout)
            for _directory in directories
        ]
        # Check pristine views before any mutation can earn a kill. With a
        # shared worker pool, one run per distinct command is enough: every
        # worker's view is built from the same layers.
        with ThreadPoolExecutor(max_workers=len(directories)) as executor:
            checks = []
            seen = set()
            for directory, first_plan in zip(directories, first_plans):
                if pool is not None and not pool.check_each_worker:
                    key = first_plan[0] if isinstance(first_plan[0], str) else tuple(first_plan[0])
                    if key in seen:
                        continue
                    seen.add(key)
                checks.append(
                    executor.submit(
                        _check_worker if pool is None else pool.check,
                        directory, root, relative, original, cwd, runner, *first_plan)
                )
            for check in checks:
                check.result()
        if pool is not None:
            pool.validate()
        completed: dict[str, str] = {}
        _run_all(
            directories,
            sites,
            completed,
            root,
            relative,
            original,
            runner,
            command,
            cwd,
            timeout,
            file_key,
            plans,
            command if check_command is None else check_command,
            reserved,
            shared_checks=set()
            if pool is not None and not pool.check_each_worker else None,
        )
        if confirmation is not None:
            full_command, full_timeout = confirmation
            survivors = [
                site for site in sites
                if completed.get(site.mutation_id) == "survived"
                and (plans or {}).get(site.mutation_id, (command, timeout))[0] != full_command
            ]
            if survivors:
                directory = directories[0]
                _check_worker(directory, root, relative, original, cwd, runner,
                              full_command, full_timeout)
                for site in survivors:
                    completed[site.mutation_id] = _execute(
                        directory, site, root, relative, original, runner,
                        full_command, cwd, full_timeout, file_key,
                        phase="survivor-confirmation",
                    )
        if pool is not None:
            pool.validate()
        outcomes.update(completed)
    except BaseException:
        if pool is not None:
            pool.close()
        raise
    finally:
        if base is not None:
            _delete_run(base)


@dataclass
class QueuedFile:
    """One file's mutants and the command that judges them.

    Commands differ per file (covering specs first), so every item carries its
    own. Sites stay in the order the engine selected them, with each file's
    previous survivors first.
    """

    relative: str
    original: bytes
    sites: list[Site]
    command: Command
    cwd: Path
    timeout: float
    file_key: str
    check_command: Command
    check_timeout: float
    completed: dict[str, str] = field(default_factory=dict)


def _check_queue(pool: WorkerPool, directories: list[Path], files: list[QueuedFile], runner) -> None:
    """Pristine checks for every distinct command before any mutant can earn a kill."""

    distinct: dict[tuple, QueuedFile] = {}
    for item in files:
        command = item.check_command
        key = (command,) if isinstance(command, str) else tuple(command)
        distinct.setdefault((key, str(item.cwd)), item)
    planned: dict[Path, list[QueuedFile]] = {}
    for index, item in enumerate(distinct.values()):
        if pool.check_each_worker:
            for directory in directories:
                planned.setdefault(directory, []).append(item)
        else:
            # One run per command; the other workers are compared structurally.
            planned.setdefault(directories[index % len(directories)], []).append(item)

    def check(directory: Path) -> None:
        for item in planned[directory]:
            pool.check(directory, pool.root, item.relative, item.original, item.cwd, runner,
                       item.check_command, item.check_timeout)

    with ThreadPoolExecutor(max_workers=len(planned)) as executor:
        for future in [executor.submit(check, directory) for directory in planned]:
            future.result()


def run_queue(pool: WorkerPool, files: list[QueuedFile], max_workers: int | None, runner, on_done) -> None:
    """Run the mutants of several files on one worker queue.

    A worker that finishes a mutant takes the next one from any file, so one
    file's slowest mutant no longer idles the rest. ``on_done(item)`` runs one
    call at a time as soon as that file's last mutant is restored and the real
    tree is validated; after any failure no further file completes. Every
    mutant runs in a fresh view, so a worker carries nothing to the next one.
    """

    files = [item for item in files if item.sites]
    if not files:
        return
    try:
        for item in files:
            if pool.sources.get(item.relative) != item.original:
                raise WorkerBaselineError("Worker pool source binding changed or pool failed")
        with measure("worker-prepare"):
            count = worker_count(sum(len(item.sites) for item in files), max_workers)
            directories = pool.prepare(files[0].relative, files[0].original, count)
        runner.workers = len(directories)
        _check_queue(pool, directories, files, runner)
        pool.validate()
        pending: Queue = Queue()
        for item in files:
            for site in item.sites:
                pending.put((item, site))
        remaining = {id(item): len(item.sites) for item in files}
        lock, publishing, stop = Lock(), Lock(), Event()

        def drain(directory: Path) -> None:
            try:
                while not stop.is_set():
                    try:
                        item, site = pending.get_nowait()
                    except Empty:
                        return
                    if runner.verbose:
                        with lock:
                            _report(item.file_key, site)
                    status = _execute(
                        directory, site, pool.root, item.relative, item.original, runner,
                        item.command, item.cwd, item.timeout, item.file_key,
                    )
                    with lock:
                        item.completed[site.mutation_id] = status
                        remaining[id(item)] -= 1
                        finished = remaining[id(item)] == 0
                    if finished:
                        with publishing:
                            if stop.is_set():
                                return
                            pool.validate()
                            on_done(item)
            except BaseException:
                stop.set()
                raise

        with ThreadPoolExecutor(max_workers=len(directories)) as executor:
            futures = [executor.submit(drain, directory) for directory in directories]
            try:
                for future in futures:
                    future.result()
            except BaseException:
                # An interrupt lands here; running mutants finish and restore.
                stop.set()
                raise
        pool.validate()
    except BaseException:
        pool.close()
        raise
