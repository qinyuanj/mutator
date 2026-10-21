"""Run a project's tests, and decide the command for each language."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
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


def _worker_home(cwd: Path) -> Path | None:
    """The overlay root when cwd is inside target/mutation-workers."""

    for candidate in [cwd, *cwd.parents]:
        parent = candidate.parent
        if not candidate.name.startswith("worker-"):
            continue
        if not parent.name.startswith("run-"):
            continue
        if parent.parent.name == "mutation-workers" and parent.parent.parent.name == "target":
            return candidate
    return None


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


def _prefer_worker_lua(cwd: Path, environment: dict) -> None:
    # These credentials describe this invocation, never an inherited worker.
    environment.pop("MUTATOR_WORKER_ROOT", None)
    environment.pop("MUTATOR_SOURCE_ROOT", None)
    worker = _worker_home(cwd.resolve())
    if worker is None:
        return
    source = worker.parents[3]
    environment["MUTATOR_WORKER_ROOT"] = worker.as_posix()
    environment["MUTATOR_SOURCE_ROOT"] = source.as_posix()
    prefix = source.as_posix().rstrip("/") + "/"
    for name in ("LUA_PATH", "LUA_PATH_5_4", "LUA_CPATH", "LUA_CPATH_5_4"):
        if name not in environment:
            continue
        entries = []
        for entry in environment[name].split(";"):
            normalized = entry.replace("\\", "/")
            inside_worker = os.path.normcase(normalized[:len(worker.as_posix()) + 1]) == os.path.normcase(worker.as_posix() + "/")
            if not inside_worker and os.path.normcase(normalized[:len(prefix)]) == os.path.normcase(prefix):
                relative = normalized[len(prefix):]
                # Dependencies are intentionally absent from the overlay.
                if relative.split("/", 1)[0].lower() != ".toolcache":
                    entry = worker.as_posix() + "/" + relative
            entries.append(entry)
        environment[name] = ";".join(entries)


def display_command(command: Command) -> str:
    """A shell-looking rendering. A list is quoted so it can be pasted."""

    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(part) for part in command)


class CommandRunner:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose

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
        _prefer_worker_lua(cwd, environment)
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
            )
        except OSError as exc:
            seconds = time.monotonic() - started
            return CommandResult(
                code=127, timed_out=False, seconds=seconds, output=str(exc), launch_error=exc
            )
        try:
            output, _err = process.communicate(timeout=timeout)
            code = process.returncode if process.returncode is not None else 1
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _err = process.communicate()
            code = 124
            timed_out = True
        seconds = time.monotonic() - started
        return CommandResult(code=code, timed_out=timed_out, seconds=seconds, output=output or "")


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


def _lua_plan(root: Path, source: Path, busted_task: str | None = None) -> tuple[Command, Path]:
    """busted in the nearest `.busted` or rockspec directory, on Lua 5.4."""

    runners = ensure_crapper().runners
    directory = next(iter(runners.lua_modules(root, [source])))
    lua = runners.lua_interpreter() or "lua5.4"
    # Preserve module paths supplied by a LuaRocks interpreter wrapper, then
    # run Busted in-process: its --lua re-exec loses quoting for spaced paths.
    loader = _lua_worker_setup() + "pcall(require, 'luarocks.loader'); "
    command = [
        lua, "-e",
        loader + "require('busted.runner')({standalone = false}); os.exit(0)",
        "--", "busted", "--ignore-lua",
    ]
    if busted_task is not None:
        command.extend(["--run", busted_task])
    return command, directory
