"""Run mutants in symlink overlays, the way clj-mutate runs its workers.

Each worker is a directory under target/mutation-workers. Project files are
linked in. Sources under test are private copies, so workers can mutate them
without touching the original tree. A Lua run can retain a fixed overlay across
files after validating restoration; other callers use per-file overlays.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock

from mutator.model import Site
from mutator.runner import Command, run_tests
from mutator.sites import apply_site
from mutator.timings import measure, record

# Build output and the worker tree itself must not be shared. A link to
# ``target`` would point a worker at the directory that contains the worker.
SKIP_LINK = frozenset(
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

CONFIGS = (
    "deps.edn",
    "bb.edn",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "go.mod",
    "go.sum",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "tsconfig.json",
    "Cargo.toml",
    "Cargo.lock",
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "setup.py",
    "conftest.py",
    "requirements.txt",
    "Pipfile",
    "poetry.lock",
    ".busted",
    ".luacov",
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
    """Remove the overlays this process created and has not removed yet."""

    for directory in list(_created_runs):
        delete_tree(directory)
        _created_runs.discard(directory)


def symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target.resolve(), target_is_directory=target.is_dir())


def _is_link(path: Path) -> bool:
    return stat.S_ISLNK(path.lstat().st_mode)


def delete_tree(path: Path) -> None:
    """Remove worker data, unlinking links without visiting their targets."""

    try:
        linked = _is_link(path)
    except FileNotFoundError:
        return
    if linked:
        path.unlink()
        return
    if path.is_dir():
        for child in list(path.iterdir()):
            delete_tree(child)
        path.rmdir()
        return
    path.unlink()


def _copy_configs(worker: Path, root: Path, relative: str) -> None:
    for name in CONFIGS:
        source = root / name
        if not source.is_file():
            continue
        if name == relative:
            continue
        (worker / name).write_bytes(source.read_bytes())


def _link_children(worker_dir: Path, real_dir: Path, skip: set[str]) -> None:
    if not real_dir.is_dir():
        return
    for child in real_dir.iterdir():
        if child.name in skip or child.name in SKIP_LINK:
            continue
        destination = worker_dir / child.name
        if destination.exists() or destination.is_symlink():
            continue
        symlink(destination, child)


# Node, and Vite in front of it, resolve a relative import from the real path
# of the module. A symlinked test therefore loads the original source.
_SPECIFIER = re.compile(
    r"""(?:from|import|require)\s*\(?\s*['"](\.[^'"]+)['"]""",
)
_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _specifiers(text: str) -> list[str]:
    found = []
    for match in _SPECIFIER.finditer(text):
        specifier = match.group(1).split("?", 1)[0].split("#", 1)[0]
        if specifier.startswith("."):
            found.append(specifier)
    return found


def _stem(base: Path) -> Path:
    if base.suffix in _EXTENSIONS:
        return Path(str(base)[: -len(base.suffix)])
    return base


def _candidates(directory: Path, specifier: str) -> list[Path]:
    base = directory / specifier
    stem = _stem(base)
    names = [base]
    for ext in _EXTENSIONS:
        names.append(Path(str(stem) + ext))
    for ext in _EXTENSIONS:
        names.append(stem / f"index{ext}")
    return names


def _inside(root: Path, path: Path) -> Path | None:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return root / relative


def _imported_files(importer: Path, root: Path) -> set[Path]:
    try:
        text = importer.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()
    found = set()
    for specifier in _specifiers(text):
        for candidate in _candidates(importer.parent, specifier):
            local = _inside(root, candidate)
            if local is not None and local.is_file():
                found.add(local.resolve())
    return found


def _keep_dir(name: str) -> bool:
    return name not in SKIP_LINK and name != "node_modules"


def _source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames
            if _keep_dir(name) and not _is_link(Path(dirpath) / name)
        ]
        for name in filenames:
            source = Path(dirpath) / name
            if source.suffix in _EXTENSIONS and not _is_link(source):
                yield source


def importers_of(root: Path, relative: str) -> set[str]:
    """Modules that reach ``relative`` through a relative import.

    Those modules have to be real files in the worker. A symlink would send
    Node back to the original tree.
    """

    target = (root / relative).resolve()
    reverse: dict[Path, set[Path]] = {}
    for source in _source_files(root):
        for imported in _imported_files(source, root):
            reverse.setdefault(imported, set()).add(source.resolve())
    found: set[Path] = set()
    pending = [target]
    while pending:
        current = pending.pop()
        for importer in reverse.get(current, ()):
            if importer in found or importer == target:
                continue
            found.add(importer)
            pending.append(importer)
    root_resolved = root.resolve()
    return {path.relative_to(root_resolved).as_posix() for path in found}


def _expand_directory(link: Path, real_dir: Path) -> None:
    """Replace a linked directory so a copied module is not written through it."""

    if not real_dir.is_dir():
        return
    link.unlink()
    link.mkdir()
    for child in real_dir.iterdir():
        if child.name in SKIP_LINK:
            continue
        symlink(link / child.name, child)


def _copy_importer(worker: Path, root: Path, relative: str) -> None:
    real = root
    current = worker
    for segment in relative.split("/")[:-1]:
        real = real / segment
        current = current / segment
        if current.is_symlink():
            _expand_directory(current, real)
        elif not current.exists():
            current.mkdir(parents=True)
    destination = worker / relative
    if destination.is_symlink():
        destination.unlink()
    elif destination.exists():
        return
    destination.write_bytes((root / relative).read_bytes())


def _overlay(worker: Path, root: Path, relative: str, original: bytes) -> None:
    """Real directories along the source path. Every sibling is a link."""

    segments = tuple(relative.split("/"))
    for index, _segment in enumerate(segments[:-1]):
        rel_dir = Path(*segments[: index + 1])
        _link_children(worker / rel_dir, root / rel_dir, {segments[index + 1]})
    destination = worker.joinpath(*segments)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(original)


def create_workers(
    base: Path, root: Path, relative: str, original: bytes, count: int,
    *, start: int = 0,
) -> list[Path]:
    created = []
    first = relative.split("/", 1)[0]
    importers = importers_of(root, relative)
    try:
        for index in range(start, start + count):
            worker = base / f"worker-{index}"
            worker.mkdir(parents=True, exist_ok=True)
            _copy_configs(worker, root, relative)
            _link_children(worker, root, {first})
            _overlay(worker, root, relative, original)
            for importer in importers:
                _copy_importer(worker, root, importer)
            created.append(worker)
    except BaseException:
        delete_tree(base)
        raise
    return created


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
    validate=None,
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
    destination = directory / relative
    if validate is not None:
        validate(directory)
    mutated = apply_site(original.decode("utf-8"), site.start, site.end, site.mutant)
    destination.write_bytes(mutated.encode("utf-8"))
    try:
        result = run_tests(runner, command, mapped_cwd(directory, root, cwd), timeout)
        record(phase, getattr(result, "seconds", 0.0))
        if runner.verbose:
            print(
                f"result {phase} {file_key}:{site.line} {site.description}: "
                f"{getattr(result, 'seconds', 0.0):.3f}s, "
                f"exit={result.code}, timed_out={result.timed_out}",
                file=sys.stderr,
            )
    finally:
        if validate is not None:
            try:
                replaced = any(
                    _is_link(path) for path in (destination, *destination.parents)
                    if path == directory or directory in path.parents
                )
            except FileNotFoundError as error:
                raise WorkerBaselineError(
                    f"Worker source binding removed in {directory}") from error
            if replaced:
                raise WorkerBaselineError(f"Worker source binding replaced in {directory}")
        destination.write_bytes(original)
    if validate is not None:
        validate(directory)
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
    validate=None,
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
                    if validate is not None:
                        validate(directory)
                    _check_worker(directory, root, cwd, runner, selected_command, selected_timeout)
                    if validate is not None:
                        validate(directory)
                else:
                    # One pristine run per command across the run; the other
                    # workers' structural state is compared by validate().
                    with lock:
                        first = key not in shared_checks
                        if first:
                            shared_checks.add(key)
                    if first:
                        if validate is not None:
                            validate(directory)
                        _check_worker(directory, root, cwd, runner, selected_command, selected_timeout)
                        if validate is not None:
                            validate(directory)
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
            validate=validate,
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
    validate=None,
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
                validate,
                shared_checks,
            )
            for index, directory in enumerate(directories)
        ]
        for future in futures:
            future.result()


class WorkerBaselineError(RuntimeError):
    """An unmutated overlay cannot execute the project's test command."""


def _check_worker(directory, root, cwd, runner, command, timeout) -> None:
    with measure("worker-check"):
        result = run_tests(runner, command, mapped_cwd(directory, root, cwd), timeout)
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


def _worker_state(directory: Path, *, project=False) -> dict[str, tuple[str, bytes | str]]:
    """Fingerprint private data and link bindings without following directory links."""
    state = {}

    def visit(folder):
        for path in folder.iterdir():
            if project and (path.name in SKIP_LINK or path.name in {"node_modules", ".claude", ".agents"}):
                continue
            key = path.relative_to(directory).as_posix()
            if _is_link(path):
                state[key] = ("link", str(path.resolve()))
                if project and path.is_dir():
                    raise WorkerBaselineError(f"Worker reuse cannot freeze linked project directory: {path}")
                if project and path.is_file():
                    state[key + "/@contents"] = ("file", hashlib.sha256(path.read_bytes()).digest())
            elif path.is_dir():
                state[key] = ("directory", "")
                visit(path)
            else:
                state[key] = ("file", hashlib.sha256(path.read_bytes()).digest())

    visit(directory)
    return state


class WorkerPool:
    """Run-scoped Lua overlays with a fixed set of private source bindings.

    Layout never changes after the first baseline. Any unexpected private data
    change poisons the pool; callers must discard it rather than trust a kill.
    Commands are still checked separately on every worker and working directory.
    """

    def __init__(self, root: Path, sources: list[Path], check_each_worker: bool = True):
        self.root = root.resolve()
        self.sources = {
            path.resolve().relative_to(self.root).as_posix(): path.read_bytes()
            for path in sources
        }
        # False: one pristine command run per run, not per worker. The other
        # workers' overlays are still compared structurally by validate().
        self.check_each_worker = check_each_worker
        self.base = None
        self.directories = []
        self.states = {}
        self.checked = set()
        self.failed = False
        self.project_state = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.base is not None:
            delete_tree(self.base)
        self.failed = True

    def prepare(self, relative, original, count):
        if self.failed or self.sources.get(relative) != original:
            raise WorkerBaselineError("Worker pool source binding changed or pool failed")
        if self.base is None:
            self.base = new_run_dir(self.root)
            try:
                self.project_state = _worker_state(self.root, project=True)
                self.directories = create_workers(
                    self.base, self.root, relative, original, count)
                for directory in self.directories:
                    for name in self.sources:
                        _copy_importer(directory, self.root, name)
                    self.states[directory] = _worker_state(directory)
            except BaseException:
                self.close()
                raise
        self.validate()
        if len(self.directories) < count:
            added = create_workers(
                self.base, self.root, relative, original,
                count - len(self.directories), start=len(self.directories))
            for directory in added:
                for name in self.sources:
                    _copy_importer(directory, self.root, name)
                self.states[directory] = _worker_state(directory)
            self.directories.extend(added)
        return self.directories[:count]

    def validate_directory(self, directory):
        if _worker_state(directory) != self.states[directory]:
            self.failed = True
            raise WorkerBaselineError(f"Worker restoration failed in {directory}")

    def validate_project(self):
        """The real tree only. Safe while workers hold mutants of other files."""
        if _worker_state(self.root, project=True) != self.project_state:
            self.failed = True
            raise WorkerBaselineError("Project inputs changed during worker reuse")
        for name, original in self.sources.items():
            if (self.root / name).read_bytes() != original:
                self.failed = True
                raise WorkerBaselineError(f"Original source changed during worker reuse: {name}")

    def validate(self):
        self.validate_project()
        for directory in self.directories:
            self.validate_directory(directory)

    def check(self, directory, root, cwd, runner, command, timeout):
        mapped = mapped_cwd(directory, root, cwd)
        if not mapped.is_relative_to(directory.resolve()):
            raise WorkerBaselineError(f"Worker working directory escapes overlay: {cwd}")
        key = (directory, str(mapped),
               ("shell", command) if isinstance(command, str) else ("argv", *command))
        if key not in self.checked:
            _check_worker(directory, root, cwd, runner, command, timeout)
            self.checked.add(key)


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
        # Validate pristine overlays before any mutation can earn a kill. With
        # a shared worker pool, one run per distinct command is enough: the
        # pool compares every other worker's structure before and after use.
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
                        directory, root, cwd, runner, *first_plan)
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
            pool.validate_directory if pool is not None else None,
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
                if pool is not None:
                    pool.validate_directory(directory)
                _check_worker(directory, root, cwd, runner, full_command, full_timeout)
                if pool is not None:
                    pool.validate_directory(directory)
                for site in survivors:
                    completed[site.mutation_id] = _execute(
                        directory, site, root, relative, original, runner,
                        full_command, cwd, full_timeout, file_key,
                        phase="survivor-confirmation",
                        validate=pool.validate_directory if pool is not None else None,
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
            delete_tree(base)


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
            pool.check(directory, pool.root, item.cwd, runner, item.check_command, item.check_timeout)

    with ThreadPoolExecutor(max_workers=len(planned)) as executor:
        for future in [executor.submit(check, directory) for directory in planned]:
            future.result()


def run_queue(pool: WorkerPool, files: list[QueuedFile], max_workers: int | None, runner, on_done) -> None:
    """Run the mutants of several files on one worker queue.

    A worker that finishes a mutant takes the next one from any file, so one
    file's slowest mutant no longer idles the rest. ``on_done(item)`` runs one
    call at a time as soon as that file's last mutant is restored and the real
    tree is validated; after any failure no further file completes. Each
    worker validates its own overlay around every mutant; the pool-wide
    validate() runs only while every worker is pristine.
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
                        validate=pool.validate_directory,
                    )
                    with lock:
                        item.completed[site.mutation_id] = status
                        remaining[id(item)] -= 1
                        finished = remaining[id(item)] == 0
                    if finished:
                        with publishing:
                            if stop.is_set():
                                return
                            pool.validate_project()
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
