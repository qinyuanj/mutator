"""Run a project's tests, and decide the command for each language."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mutator.crapper_link import ensure_crapper

Command = str | list[str]


@dataclass(frozen=True)
class CommandResult:
    code: int
    timed_out: bool
    seconds: float
    output: str
    launch_error: OSError | None = None


class Interrupted(BaseException):
    """SIGTERM or SIGINT reached mutator. No outcome after it can be trusted."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


# Test process groups still running, so an interrupt can kill them.
_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()
_stopped: int | None = None


def _kill_group(process: subprocess.Popen, signum: int = signal.SIGKILL) -> bool:
    """Signal the command's process group; False when the group is empty."""

    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        return False
    return True


def _group_alive(process: subprocess.Popen) -> bool:
    return _kill_group(process, 0)


# How long a command's leftover children get to handle SIGTERM.
REAP_GRACE = 1.0


def _wait_unreaped(process: subprocess.Popen, timeout: float | None) -> bool:
    """Wait for the leader to exit but leave it a zombie; False on timeout.

    A zombie leader keeps its pid, so its group id cannot be reused by an
    unrelated session while the leftovers are signalled.
    """

    if not hasattr(os, "waitid"):
        # Without waitid the leader is reaped here, so the window stays open.
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True
    deadline = None if timeout is None else time.monotonic() + timeout
    delay = 0.0005
    while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(delay)
        delay = min(delay * 2, 0.05)
    return True


def _reap_group(process: subprocess.Popen) -> None:
    """End what the exited command left in its group: SIGTERM, then SIGKILL.

    The first signal goes out while the leader is an unreaped zombie, which
    closes the pid-reuse window for it. Later probes run after the leader is
    reaped and rely on the leftovers holding the group id; only a pid wrap
    within one poll interval could misdirect them.
    """

    _kill_group(process, signal.SIGTERM)
    process.wait()
    deadline = time.monotonic() + REAP_GRACE
    delay = 0.005
    while _group_alive(process):
        if time.monotonic() >= deadline:
            _kill_group(process)
            return
        time.sleep(delay)
        delay = min(delay * 2, 0.05)


def stop_commands(signum: int) -> None:
    """Kill every running command; later and unfinished ones raise Interrupted.

    Called from a signal handler on the main thread, so it takes no lock the
    main thread could already hold.
    """

    global _stopped
    _stopped = signum
    for process in list(_running):
        _kill_group(process)


def _check_stopped() -> None:
    if _stopped is not None:
        raise Interrupted(_stopped)


# A Rust worker slot's directory name, followed by its number.
SLOT_PREFIX = "cargo-slot-"


def _worker_home(cwd: Path) -> Path | None:
    """The overlay root when cwd is inside target/mutation-workers.

    A run overlay is ``run-<uuid>/worker-<n>``; a Rust worker slot's is
    ``cargo-slot-<n>/worker``.
    """

    for candidate in [cwd, *cwd.parents]:
        parent = candidate.parent
        run = candidate.name.startswith("worker-") and parent.name.startswith("run-")
        slot = candidate.name == "worker" and parent.name.startswith(SLOT_PREFIX)
        if not (run or slot):
            continue
        if parent.parent.name == "mutation-workers" and parent.parent.parent.name == "target":
            return candidate
    return None


def _use_slot_target(cwd: Path, environment: dict) -> None:
    """Point Cargo in a worker slot at that slot's own, kept build output."""

    worker = _worker_home(cwd.resolve())
    if worker is None or not worker.parent.name.startswith(SLOT_PREFIX):
        return
    target = str(worker.parent / "target")
    environment["CARGO_TARGET_DIR"] = target
    # A configured build-dir would otherwise be shared by every slot.
    environment["CARGO_BUILD_BUILD_DIR"] = target


def _source_entries(cwd: Path, worker: Path) -> list[str]:
    found = []
    for directory in (cwd / "src", cwd, worker / "src", worker):
        text = str(directory)
        if text in found:
            continue
        if directory.is_dir():
            found.append(text)
    return found


def _prefer_worker_sources(cwd: Path, environment: dict) -> None:
    """An editable install would otherwise import the unmutated tree."""

    worker = _worker_home(cwd)
    if worker is None:
        return
    entries = _source_entries(cwd, worker)
    current = environment.get("PYTHONPATH")
    if current:
        entries.append(current)
    if not entries:
        return
    environment["PYTHONPATH"] = os.pathsep.join(entries)


def _rebase_lua_paths(value: str, worker: Path, source: Path) -> str:
    prefix = source.as_posix().rstrip("/") + "/"
    entries = []
    for entry in value.split(";"):
        normalized = entry.replace("\\", "/")
        inside_worker = os.path.normcase(normalized[:len(worker.as_posix()) + 1]) == os.path.normcase(worker.as_posix() + "/")
        if not inside_worker and os.path.normcase(normalized[:len(prefix)]) == os.path.normcase(prefix):
            relative = normalized[len(prefix):]
            # Dependencies are intentionally absent from the overlay.
            if relative.split("/", 1)[0].lower() != ".toolcache":
                entry = worker.as_posix() + "/" + relative
        entries.append(entry)
    return ";".join(entries)


_LUA_PATH_NAMES = ("LUA_PATH", "LUA_PATH_5_4", "LUA_CPATH", "LUA_CPATH_5_4")


def _prefer_worker_lua(cwd: Path, environment: dict, command: Command) -> Command:
    # These credentials describe this invocation, never an inherited worker.
    environment.pop("MUTATOR_WORKER_ROOT", None)
    environment.pop("MUTATOR_SOURCE_ROOT", None)
    worker = _worker_home(cwd.resolve())
    if worker is None:
        return command
    source = worker.parents[3]
    environment["MUTATOR_WORKER_ROOT"] = worker.as_posix()
    environment["MUTATOR_SOURCE_ROOT"] = source.as_posix()
    for name in _LUA_PATH_NAMES:
        if name in environment:
            environment[name] = _rebase_lua_paths(environment[name], worker, source)
    if isinstance(command, list) and command and command[0] == "env":
        # Assignments embedded in an `env VAR=...` prefix are what the test
        # process and its children inherit; rebase them like the environment.
        rebased = []
        for part in command:
            name, separator, value = part.partition("=")
            if separator and "/" not in name and name in _LUA_PATH_NAMES:
                part = name + "=" + _rebase_lua_paths(value, worker, source)
            rebased.append(part)
        return rebased
    return command


_LUA_MEMORY_MB = 2048


def _memory_limit(command: Command):
    """Cap a generated Lua run's address space so a runaway mutant fails fast.

    A mutant stuck in an allocating loop otherwise grows until its timeout and
    can exhaust the machine. The pristine suite peaks well under the cap; a
    mutant that hits it raises "not enough memory" and fails like any other
    error. `MUTATOR_LUA_MEMORY_MB=0` disables the cap.
    """

    if not isinstance(command, list) or not command or os.name != "posix":
        return None
    if not Path(lua_executable(command)).name.startswith("lua"):
        return None
    megabytes = int(os.environ.get("MUTATOR_LUA_MEMORY_MB", _LUA_MEMORY_MB))
    if megabytes <= 0:
        return None
    import resource

    limit = megabytes * 1024 * 1024

    def apply():
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return apply


def display_command(command: Command) -> str:
    """A shell-looking rendering. A list is quoted so it can be pasted."""

    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(part) for part in command)


def _share_cores(cwd: Path, environment: dict, workers: int) -> None:
    """Split the cores between a run's workers; a user's own setting wins.

    Cargo otherwise starts one job per core in every worker at once.
    """

    if "CARGO_BUILD_JOBS" in os.environ or _worker_home(cwd.resolve()) is None:
        return
    environment["CARGO_BUILD_JOBS"] = str(max(1, (os.cpu_count() or 1) // max(1, workers)))


class CommandRunner:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        # Set by the worker scheduler to the number of workers of the run.
        self.workers = 1

    def run(self, command: Command, cwd: Path, timeout: float | None) -> CommandResult:
        """Run `command` in `cwd`.

        A list is an argument vector and does not go through a shell, so a
        path with spaces or metacharacters stays one argument. A string is a
        command the user typed in `--test-command` or `--coverage-command`.
        """

        if self.verbose:
            print(f"+ ({cwd}) {display_command(command)}", file=sys.stderr)
        started = time.monotonic()
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        _prefer_worker_sources(cwd, environment)
        _share_cores(cwd, environment, self.workers)
        _use_slot_target(cwd, environment)
        command = _prefer_worker_lua(cwd, environment, command)
        _check_stopped()
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                shell=isinstance(command, str),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
                env=environment,
                preexec_fn=_memory_limit(command),
            )
        except OSError as exc:
            seconds = time.monotonic() - started
            return CommandResult(
                code=127, timed_out=False, seconds=seconds, output=str(exc), launch_error=exc
            )
        with _running_lock:
            _running.add(process)
        # Read on a thread rather than through communicate(), which would reap
        # the leader before its leftovers can be signalled. The reader closes
        # the pipe itself: closing it here would block on a reader that an
        # interrupt left waiting for a detached holder of the pipe.
        chunks: list[str] = []
        failures: list[BaseException] = []

        def read() -> None:
            try:
                with process.stdout:
                    chunks.append(process.stdout.read())
            except BaseException as exc:  # Raised in run(), as communicate() would.
                failures.append(exc)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            if _stopped is not None:
                # The interrupt came between the launch and the registration.
                _kill_group(process)
            deadline = None if timeout is None else time.monotonic() + timeout
            reader.join(timeout)
            timed_out = reader.is_alive() or not _wait_unreaped(
                process, None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if timed_out:
                _kill_group(process)
                reader.join()
                process.wait()
                code = 124
            else:
                seconds = time.monotonic() - started
                _reap_group(process)
                code = process.returncode if process.returncode is not None else 1
        finally:
            with _running_lock:
                _running.discard(process)
        # A command killed by the interrupt has no outcome, not a kill.
        _check_stopped()
        if failures:
            raise failures[0]
        if timed_out:
            seconds = time.monotonic() - started
        return CommandResult(
            code=code, timed_out=timed_out, seconds=seconds, output="".join(chunks)
        )


def build_command(command: Command) -> Command | None:
    """The compile step that a generated test command performs before its tests."""

    if isinstance(command, list) and command[:2] == ["cargo", "test"]:
        return ["cargo", "test", "--no-run", *command[2:]]
    return None


def run_tests(runner, command: Command, cwd: Path, timeout: float | None) -> CommandResult:
    """Build untimed, then time only the tests.

    The timeout is a multiple of a warm baseline. A worker's first cargo build
    is cold, and charging it to the timeout turns survivors into kills. A build
    that fails still fails the command.
    """

    build = build_command(command)
    if build is not None:
        built = runner.run(build, cwd, None)
        if built.launch_error is not None or built.code != 0:
            return built
    return runner.run(command, cwd, timeout)


def nearest(start: Path, marker: str, stop: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    stop = stop.resolve()
    current = current.resolve()
    while True:
        if (current / marker).is_file():
            return current
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _clojure_command(directory: Path) -> list[str]:
    deps = directory / "deps.edn"
    bb = directory / "bb.edn"
    if bb.is_file() and not deps.is_file():
        text = _read(bb)
        if "spec" in text:
            return ["bb", "spec", "--tag", "~no-mutate"]
        return ["bb", "test"]
    text = _read(deps)
    if ":spec" in text and "speclj" in text:
        return ["clj", "-M:spec", "--tag", "~no-mutate"]
    if ":spec" in text:
        return ["clj", "-M:spec"]
    return ["clj", "-M:test"]


def _project_interpreter(directory: Path) -> str:
    """The project's virtualenv, or this process when the project has none.

    The path stays absolute and the symlink is left in place. A worker does
    not link ``.venv``, and resolving ``bin/python`` would leave the virtualenv.
    """

    for name in (".venv", "venv"):
        candidate = (directory / name / "bin" / "python").absolute()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return sys.executable


def _python_command(directory: Path) -> list[str]:
    interpreter = _project_interpreter(directory)
    pyproject = _read(directory / "pyproject.toml")
    if (
        (directory / "pytest.ini").is_file()
        or (directory / "conftest.py").is_file()
        or (directory / "tests" / "conftest.py").is_file()
        or (directory / "test" / "conftest.py").is_file()
        or "pytest" in pyproject
        or (directory / "setup.cfg").is_file() and "pytest" in _read(directory / "setup.cfg")
    ):
        return [interpreter, "-m", "pytest"]
    return [interpreter, "-m", "unittest", "discover"]


def test_plan(
    root: Path, source: Path, language: str, override: str | None,
    busted_task: str | None = None,
) -> tuple[Command, Path]:
    """The test command and the directory it runs in.

    A user override stays a shell string. Every generated command is an
    argument vector, so a package path is one argument.
    """

    root = root.resolve()
    if override:
        return override, root
    if language == "clojure":
        directory = nearest(source, "deps.edn", root) or nearest(source, "bb.edn", root) or root
        return _clojure_command(directory), directory
    if language == "java":
        directory = nearest(source, "pom.xml", root) or root
        return ["mvn", "-q", "test", "-DexcludeTags=no-mutate"], directory
    if language == "go":
        directory = nearest(source, "go.mod", root) or root
        relative = source.resolve().parent.relative_to(directory.resolve())
        package = "." if relative == Path(".") else "./" + relative.as_posix()
        return ["go", "test", "-count=1", package], directory
    if language == "typescript":
        directory = nearest(source, "package.json", root) or root
        return ["npm", "test"], directory
    if language == "rust":
        directory = nearest(source, "Cargo.toml", root) or root
        return ["cargo", "test"], directory
    if language == "python":
        directory = (
            nearest(source, "pyproject.toml", root)
            or nearest(source, "pytest.ini", root)
            or nearest(source, "setup.cfg", root)
            or root
        )
        return _python_command(directory), directory
    if language == "lua":
        return _lua_plan(root, source, busted_task)
    return ["false"], root


def _lua_worker_setup() -> str:
    # A wrapper may have embedded the original paths in -e, after Lua read env.
    return (
        "do local source, worker = os.getenv('MUTATOR_SOURCE_ROOT'), os.getenv('MUTATOR_WORKER_ROOT'); "
        "if source and worker then "
        "local prefix = source:gsub('\\\\', '/'):gsub('/+$', '') .. '/'; "
        "local function rebase(paths) local entries = {}; "
        "for entry in (paths .. ';'):gmatch('(.-);') do "
        "local normalized = entry:gsub('\\\\', '/'); "
        "if normalized:sub(1, #worker + 1):lower() ~= (worker .. '/'):lower() "
        "and normalized:sub(1, #prefix):lower() == prefix:lower() then "
        "local relative = normalized:sub(#prefix + 1); "
        "if (relative:match('^[^/]+') or ''):lower() ~= '.toolcache' then "
        "entry = worker .. '/' .. relative end end; "
        "entries[#entries + 1] = entry end; return table.concat(entries, ';') end; "
        "package.path = rebase(package.path); package.cpath = rebase(package.cpath) "
        "end end; "
    )


_LUA_TREES: list[str] | None = None


def _rocks_trees() -> list[str]:
    """The machine's LuaRocks 5.4 trees, or [] when luarocks is unusable."""

    global _LUA_TREES
    if _LUA_TREES is None:
        _LUA_TREES = []
        exe = shutil.which("luarocks")
        result = None
        if exe:
            try:
                result = subprocess.run(
                    [exe, "--lua-version", "5.4", "config", "rocks_trees"],
                    capture_output=True, text=True, timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                result = None
        if result is not None and result.returncode == 0:
            _LUA_TREES = [
                match for match in re.findall(r'"([^"]+)"', result.stdout)
                if match.startswith("/")
            ]
    return _LUA_TREES


def _pinned_lua_paths(directory: Path) -> tuple[str, str] | None:
    """Search templates confined to the LuaRocks trees, or None to keep Lua's.

    Stock distribution templates (/usr/share/lua) are dropped: projects that
    confine modules to controlled trees reject them, and LuaRocks-installed
    modules resolve from the trees. Inherited LUA_PATH/LUA_CPATH entries are
    kept when they are relative or inside a tree. Nothing is pinned unless
    busted itself resolves inside a tree, so a busted installed elsewhere
    keeps working with Lua's default paths. MUTATOR_LUA_NO_PIN=1 opts out.
    """

    if os.environ.get("MUTATOR_LUA_NO_PIN") == "1":
        return None
    trees = _rocks_trees()
    if not trees or not any(
        (Path(tree) / "share/lua/5.4/busted/runner.lua").is_file() for tree in trees
    ):
        return None

    def keep(entry: str) -> bool:
        return not entry.startswith("/") or any(
            entry == tree or entry.startswith(tree + "/") for tree in trees
        )

    def filtered(*names: str) -> list[str]:
        entries = []
        for name in names:
            value = os.environ.get(name)
            if value:
                entries.extend(entry for entry in value.split(";") if entry and keep(entry))
        return entries

    path = filtered("LUA_PATH_5_4", "LUA_PATH")
    cpath = filtered("LUA_CPATH_5_4", "LUA_CPATH")
    # Absolute module-directory templates: relative ones cannot be validated
    # against the project root, and the worker rebase maps these into overlays.
    root = directory.as_posix()
    path += [f"{root}/?.lua", f"{root}/?/init.lua"]
    cpath.append(f"{root}/?.so")
    for tree in trees:
        path += [f"{tree}/share/lua/5.4/?.lua", f"{tree}/share/lua/5.4/?/init.lua"]
        cpath += [f"{tree}/lib/lua/5.4/?.so", f"{tree}/lib/lua/5.4/loadall.so"]
    return ";".join(dict.fromkeys(path)), ";".join(dict.fromkeys(cpath))


def lua_executable(command: Command) -> str:
    """The interpreter a generated Lua command runs, past any env assignment prefix."""

    if isinstance(command, str):
        return command
    index = 0
    if command and command[0] == "env":
        index = 1
        while index < len(command) and "=" in command[index].split("/")[0]:
            index += 1
    return command[index]


def _lua_literal(text: str) -> str:
    # Long strings preserve backslashes, non-ASCII and spaces on every platform.
    equals = "="
    while "]" + equals + "]" in text:
        equals += "="
    return "[" + equals + "[" + text + "]" + equals + "]"


def _lua_plan(root: Path, source: Path, busted_task: str | None = None) -> tuple[Command, Path]:
    """busted in the nearest `.busted` or rockspec directory, on Lua 5.4."""

    runners = ensure_crapper().runners
    directory = next(iter(runners.lua_modules(root, [source])))
    lua = runners.lua_interpreter() or "lua5.4"
    # Preserve module paths supplied by a LuaRocks interpreter wrapper, then
    # run Busted in-process: its --lua re-exec loses quoting for spaced paths.
    support_dir = Path(__file__).resolve().parent
    loader = (
        _lua_worker_setup()
        + "local mutator_support = dofile(" + _lua_literal((support_dir / "lua_support.lua").as_posix()) + ")"
        + "(" + _lua_literal(support_dir.as_posix()) + "); "
        + "pcall(require, 'luarocks.loader'); "
        + "mutator_support.install(); "
    )
    command = [lua]
    pinned = _pinned_lua_paths(directory)
    if pinned is not None:
        # Pinned through the environment, not an in-process assignment, so
        # nested test subprocesses inherit the same confined search path. The
        # worker rebase in _prefer_worker_lua maps it into overlays.
        command = ["env", f"LUA_PATH={pinned[0]}", f"LUA_CPATH={pinned[1]}", lua]
    command += [
        "-e",
        loader + "require('busted.runner')({standalone = false}); os.exit(0)",
        "--", "busted", "--ignore-lua",
    ]
    if busted_task is not None:
        command.extend(["--run", busted_task])
    return command, directory
