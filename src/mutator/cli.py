"""Command line for the multi-language mutation tool."""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from mutator.crapper_link import ensure_crapper
from mutator.coverage import covered_lines
from mutator.engine import mutate_file, mutate_queued, restore_backups, scan_file
from mutator.report import format_results, format_site_log
from mutator.runner import CommandRunner, Interrupted, stop_commands
from mutator import timings
from mutator.workers import WorkerPool, WorkerSlots, discard_overlays

HELP = """\
Usage: mutator [options] [path-or-filter ...]

Discover mutation sites in Clojure, Java, Go, TypeScript, Rust, Python, and Lua,
run the project's tests against each one, and write `.metrics/mutate` for
uml-viewer. Namespaces and function names are the ones crapper writes into
`.metrics/crap.edn`.

A form id is `defn/<name>` or, for a private operation, `defn-/<name>`.
uml-viewer joins that name to the operation and joins `:namespace` to the
class.

Options:
  -h, --help                    Print this help and exit.
  --root <path>                 Project root. Metrics are written here.
                                Default: the current directory.
  -s, --source-root <path>      Walk this tree instead of the project root.
                                May be repeated.
  --changed                     Mutate added and modified source files from
                                git status.
  --scan                        List mutation sites. Do not run tests or
                                write metrics.
  --mutate-all                  Run every covered site, including ones the
                                last snapshot already killed.
  --since-last-run              Run survivors and sites in new or rewritten
                                functions. This is the default when a snapshot
                                exists.
  --lines <n,n,...>             Run only mutations on these source lines.
  --no-coverage                 Treat every site as covered.
  --use-existing-coverage       Read coverage already on disk. Same as
                                --reuse-coverage.
  --reuse-coverage              Read coverage already on disk. --select-specs
                                recollects a missing or invalid Lua spec index.
  --coverage-command <cmd>      Run this command instead of the per-language
                                coverage tools, then read the reports it wrote.
  --test-command <cmd>          Run this command for the baseline and every
                                mutant. The working directory is the project
                                root. Cannot be combined with --busted-task.
  --busted-task <name>          Select a named task in .busted for Lua tests
                                and spec-file coverage (for example, fast).
                                Other languages are unchanged.
                                Cannot be combined with --test-command; put
                                --run <name> in your custom command instead.
  --timeout-factor <number>     Mutant timeout, as a multiple of the baseline
                                duration. Default: 10. The timeout is at least
                                2 seconds.
  --mutation-warning <number>   Warn when a file selects more covered sites
                                than this. Default: 50.
  --max-workers <number>        Run at most this many mutants at once.
                                Default: one per core. The run uses the
                                smaller of this limit, the cores, and the
                                number of selected sites.
  --select-specs                Opt into Lua spec-file selection. Requires tests
                                whose behavior is independent of other spec files;
                                shared state can change mutation outcomes.
  --full-suite                  Run all tests for every mutant (the default).
                                Cannot be combined with --select-specs.
  --paranoid-workers            Run the pristine worker check in every worker
                                instead of once per command. The other workers
                                are always compared structurally.
  --timings                     Print phase totals and mean mutant duration.
  --verbose                     Print each test command and each mutant.

Arguments:
  path              File or directory to mutate. Test paths are skipped.
  filter            When the argument is not a path, only source files whose
                    path contains this text are mutated.

With no paths, source files under the project root are mutated. Directories
named test, tests, spec, specs, vendor, node_modules, and target are skipped.

The default, once a snapshot exists, reruns survivors and sites in functions
whose text changed. Killed mutants in unchanged functions are kept.

Selected mutants of one file run at the same time, one worker per core unless
--max-workers says otherwise. Full-suite Lua files share one worker queue, so
mutants of the next file start while the current file finishes; each file's
snapshot is written when its own mutants are done. A worker is a symlink
overlay under target/mutation-workers with its own copy of the mutated file. The project tree
is left unchanged. Rust workers keep their Cargo build output in
target/mutation-workers/cargo-slot-<n>/target across runs; cargo clean
removes it. A copy left under target/mutator-backup/ by an interrupted
older run is restored before a non-scan run.

Exit codes:
  0  every executed mutant was killed, or there was nothing to run
  1  usage error
  2  baseline tests failed
  3  at least one mutant survived

Coverage, when it is produced, uses the same commands as crapper:
  Clojure      clj -M:cov --lcov
  Java         Maven JaCoCo
  Go           go test ./... -coverprofile=...
  TypeScript   npm run coverage, Vitest, or c8
  Rust         cargo llvm-cov or cargo tarpaulin
  Python       coverage.py LCOV
  Lua          busted -c with luacov, then luacov -r lcov

A site on a line the report does not mark as hit is uncovered and is not run.
"""


@dataclass
class Options:
    action: str
    message: str = ""
    exit_code: int = 0
    project_root: Path = field(default_factory=lambda: Path("."))
    source_roots: list[str] = field(default_factory=list)
    positionals: list[str] = field(default_factory=list)
    scan: bool = False
    mutate_all: bool = False
    since_last_run: bool = False
    lines: set[int] | None = None
    no_coverage: bool = False
    use_existing_coverage: bool = False
    coverage_command: str | None = None
    test_command: str | None = None
    busted_task: str | None = None
    timeout_factor: float = 10.0
    mutation_warning: int = 50
    max_workers: int | None = None
    changed: bool = False
    verbose: bool = False
    timings: bool = False
    full_suite: bool = False
    select_specs: bool = False
    paranoid_workers: bool = False


def _take(args: list[str], index: int, option: str) -> str:
    if index + 1 >= len(args) or not args[index + 1] or args[index + 1].startswith("-"):
        raise ValueError(f"{option} requires a value")
    return args[index + 1]


def _workers(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise ValueError("--max-workers requires a positive integer") from None
    if count < 1:
        raise ValueError("--max-workers requires a positive integer")
    return count


def _lines(value: str) -> set[int]:
    found = set()
    for piece in value.split(","):
        piece = piece.strip()
        if not piece:
            continue
        number = int(piece)
        if number < 1:
            raise ValueError("--lines requires positive line numbers")
        found.add(number)
    if not found:
        raise ValueError("--lines requires a line number")
    return found


def parse_args(argv: list[str] | None = None) -> Options:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in {"-h", "--help"} for arg in args):
        return Options(action="help", message=HELP, exit_code=0)
    options = Options(action="mutate")
    index = 0
    try:
        while index < len(args):
            arg = args[index]
            if arg in {"-s", "--source-root"}:
                options.source_roots.append(_take(args, index, arg))
                index += 2
                continue
            if arg == "--root":
                options.project_root = Path(_take(args, index, arg))
                index += 2
                continue
            if arg == "--coverage-command":
                options.coverage_command = _take(args, index, arg)
                index += 2
                continue
            if arg == "--test-command":
                options.test_command = _take(args, index, arg)
                index += 2
                continue
            if arg == "--busted-task":
                options.busted_task = _take(args, index, arg)
                index += 2
                continue
            if arg == "--timeout-factor":
                options.timeout_factor = float(_take(args, index, arg))
                if options.timeout_factor <= 0:
                    raise ValueError("--timeout-factor requires a positive number")
                index += 2
                continue
            if arg == "--mutation-warning":
                options.mutation_warning = int(_take(args, index, arg))
                index += 2
                continue
            if arg == "--max-workers":
                options.max_workers = _workers(_take(args, index, arg))
                index += 2
                continue
            if arg == "--lines":
                options.lines = _lines(_take(args, index, arg))
                index += 2
                continue
            if arg == "--no-coverage":
                options.no_coverage = True
                index += 1
                continue
            if arg in {"--use-existing-coverage", "--reuse-coverage"}:
                options.use_existing_coverage = True
                index += 1
                continue
            if arg == "--changed":
                options.changed = True
                index += 1
                continue
            if arg == "--scan":
                options.scan = True
                index += 1
                continue
            if arg == "--mutate-all":
                options.mutate_all = True
                index += 1
                continue
            if arg == "--since-last-run":
                options.since_last_run = True
                index += 1
                continue
            if arg == "--select-specs":
                options.select_specs = True
                index += 1
                continue
            if arg == "--full-suite":
                options.full_suite = True
                index += 1
                continue
            if arg == "--paranoid-workers":
                options.paranoid_workers = True
                index += 1
                continue
            if arg == "--timings":
                options.timings = True
                index += 1
                continue
            if arg == "--verbose":
                options.verbose = True
                index += 1
                continue
            if arg.startswith("-"):
                raise ValueError(f"Unknown option: {arg}")
            options.positionals.append(arg)
            index += 1
    except ValueError as exc:
        return Options(action="help", message=f"{exc}\n\n{HELP}", exit_code=1)
    if options.select_specs and options.full_suite:
        return Options(
            action="help",
            message=f"--select-specs cannot be combined with --full-suite\n\n{HELP}",
            exit_code=1,
        )
    if options.busted_task is not None and options.test_command is not None:
        return Options(
            action="help",
            message=f"--busted-task cannot be combined with --test-command\n\n{HELP}",
            exit_code=1,
        )
    if options.mutate_all and options.since_last_run:
        return Options(
            action="help",
            message=f"--mutate-all cannot be combined with --since-last-run\n\n{HELP}",
            exit_code=1,
        )
    if options.no_coverage and options.coverage_command:
        return Options(
            action="help",
            message=f"--no-coverage cannot be combined with --coverage-command\n\n{HELP}",
            exit_code=1,
        )
    if options.scan and options.mutate_all:
        return Options(
            action="help",
            message=f"--scan cannot be combined with --mutate-all\n\n{HELP}",
            exit_code=1,
        )
    if options.scan and options.max_workers is not None:
        return Options(
            action="help",
            message=f"--scan cannot be combined with --max-workers\n\n{HELP}",
            exit_code=1,
        )
    return options


def _changed_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or "git status failed", file=sys.stderr)
        return []
    found: list[Path] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path_text = line[3:].strip()
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        path_text = path_text.strip('"')
        found.append((root / path_text).resolve())
    return found


def _positionals(root: Path, args: list[str]) -> tuple[list[Path], list[str]]:
    existing: list[Path] = []
    filters: list[str] = []
    for arg in args:
        candidate = Path(arg)
        if not candidate.is_absolute():
            candidate = root / arg
        if candidate.exists():
            existing.append(candidate.resolve())
        else:
            filters.append(arg)
    return existing, filters


def _changed_sources(root: Path, crapper) -> list[Path]:
    files = []
    for path in _changed_files(root):
        if crapper.discover.language_of(path) is None:
            continue
        if crapper.discover.is_test_file(path):
            continue
        files.append(path)
    return files


def _is_source(path: Path, crapper) -> bool:
    if crapper.discover.language_of(path) is None:
        return False
    return not crapper.discover.is_test_file(path)


def _explicit_sources(existing: list[Path], crapper) -> list[Path]:
    files = []
    for path in existing:
        if path.is_dir():
            files.extend(crapper.discover.iter_source_files([path]))
            continue
        if _is_source(path, crapper):
            files.append(path)
            continue
        if crapper.discover.is_test_file(path):
            print(f"Skipping test file {path}", file=sys.stderr)
    return files


def _choose_files(options: Options, root: Path, existing: list[Path], crapper) -> list[Path]:
    if options.changed:
        return _changed_sources(root, crapper)
    if options.source_roots:
        return crapper.discover.iter_source_files([(root / path).resolve() for path in options.source_roots])
    if existing:
        return _explicit_sources(existing, crapper)
    return crapper.discover.iter_source_files([root])


def _matching(files: list[Path], filters: list[str]) -> list[Path]:
    if not filters:
        return files
    return [path for path in files if any(item in path.as_posix() for item in filters)]


def _inside_root(files: list[Path], root: Path) -> list[Path]:
    inside = []
    for path in files:
        try:
            path.resolve().relative_to(root)
        except ValueError:
            print(f"Skipping {path}; it is outside {root}", file=sys.stderr)
            continue
        inside.append(path.resolve())
    return inside


def select_files(options: Options) -> list[Path]:
    crapper = ensure_crapper()
    root = options.project_root.resolve()
    existing, filters = _positionals(root, options.positionals)
    files = _choose_files(options, root, existing, crapper)
    return sorted(set(_inside_root(_matching(files, filters), root)), key=lambda item: item.as_posix())


def _prepare_coverage(options: Options, root: Path, files: list[Path]) -> dict:
    if options.no_coverage or options.scan:
        return {}
    crapper = ensure_crapper()
    indexes = {}
    remaining = files
    if not (options.test_command or options.coverage_command):
        from mutator.lua_coverage import collect

        lua_files = [path for path in files if path.suffix == ".lua"]
        for directory, targets in crapper.runners.lua_modules(root, lua_files).items():
            indexes[directory] = collect(
                root, targets[0], busted_task=options.busted_task,
                reuse=options.use_existing_coverage, verbose=options.verbose, source_files=targets,
            )
        remaining = [path for path in files if path.suffix != ".lua"]
    if not options.use_existing_coverage and (remaining or not files):
        crapper.runners.run_coverage(root, remaining, options.coverage_command)
    return indexes


def _coverage_for(options: Options, root: Path, path: Path) -> set[int] | None:
    if options.no_coverage:
        return None
    crapper = ensure_crapper()
    language = crapper.discover.language_of(path)
    if language is None:
        return None
    return covered_lines(root, path, language)


def _print_help(options: Options) -> int:
    stream = sys.stdout if options.exit_code == 0 else sys.stderr
    ending = "" if options.message.endswith("\n") else "\n"
    print(options.message, file=stream, end=ending)
    return options.exit_code


def _require_crapper() -> str | None:
    try:
        ensure_crapper()
    except ImportError as exc:
        return str(exc)
    return None


def _restore(root: Path) -> None:
    for path in restore_backups(root):
        print(f"Restored {path} from an interrupted mutation.", file=sys.stderr)


def _scan(options: Options, root: Path, files: list[Path]) -> int:
    for path in files:
        print(
            scan_file(
                path,
                root,
                covered_lines=_coverage_for(options, root, path),
                ignore_coverage=options.no_coverage,
                lines=options.lines,
            ),
            end="",
        )
    return 0


def _record(result, forms: list, written: list[str]) -> str:
    if result.skipped:
        print(f"Skipping {result.path}: {result.skipped}", file=sys.stderr)
        return "skip"
    if result.baseline_failed:
        print(result.baseline_message, file=sys.stderr)
        return "baseline"
    print(format_site_log(result.sites, result.statuses), end="")
    forms.extend(result.forms)
    written.extend(result.written)
    if any(form.survived for form in result.forms):
        return "survived"
    return "ok"


def _finish(baseline_failed: bool, survived: bool) -> int:
    if baseline_failed:
        return 2
    if survived:
        return 3
    return 0


def _lua_index(root: Path, path: Path, indexes: dict | None):
    if not indexes or path.suffix != ".lua":
        return None
    directory = next(iter(ensure_crapper().runners.lua_modules(root, [path])))
    return indexes.get(directory)


def _shares_queue(options: Options, path: Path) -> bool:
    """Full-suite Lua files share one worker queue; the rest go file by file."""

    return path.suffix == ".lua" and not options.test_command and not options.select_specs


def _mutate_files(options: Options, root: Path, files: list[Path], indexes: dict | None = None) -> int:
    runner = CommandRunner(verbose=options.verbose)
    baselines: dict[tuple[tuple[str, ...], str], tuple[bool, float, str]] = {}
    forms = []
    written: list[str] = []
    baseline_failed = False
    survived = False
    results = {}
    lua_sources = [path for path in files if path.suffix == ".lua"]
    with (WorkerPool(root, lua_sources, check_each_worker=options.paranoid_workers) as worker_pool,
          WorkerSlots(root, check_each_worker=options.paranoid_workers) as worker_slots):
        queued = [path for path in files if _shares_queue(options, path)]
        if queued:
            results.update(zip(queued, mutate_queued(
                [(path, _coverage_for(options, root, path), _lua_index(root, path, indexes))
                 for path in queued],
                root,
                runner=runner,
                ignore_coverage=options.no_coverage,
                mutate_all=options.mutate_all,
                lines=options.lines,
                timeout_factor=options.timeout_factor,
                mutation_warning=options.mutation_warning,
                baselines=baselines,
                max_workers=options.max_workers,
                busted_task=options.busted_task,
                worker_pool=worker_pool,
            )))
        for path in files:
            if path in results:
                continue
            results[path] = mutate_file(
                path,
                root,
                runner=runner,
                covered_lines=_coverage_for(options, root, path),
                ignore_coverage=options.no_coverage,
                mutate_all=options.mutate_all,
                lines=options.lines,
                test_command=options.test_command,
                timeout_factor=options.timeout_factor,
                mutation_warning=options.mutation_warning,
                baselines=baselines,
                max_workers=options.max_workers,
                busted_task=options.busted_task,
                lua_coverage=_lua_index(root, path, indexes),
                worker_pool={".lua": worker_pool, ".rs": worker_slots}.get(path.suffix),
                select_specs=options.select_specs,
            )
    for path in files:
        outcome = _record(results[path], forms, written)
        if outcome == "baseline":
            baseline_failed = True
        elif outcome == "survived":
            survived = True
    if forms:
        print(format_results(forms), end="")
    for path in written:
        print(f"Wrote {path}", file=sys.stderr)
    return _finish(baseline_failed, survived)


UNSUPPORTED_PLATFORM = (
    "mutator does not support native Windows. Run it inside WSL2 with the "
    "project under ~/; see the README."
)


def platform_warning(platform: str, root: Path) -> str | None:
    """Refuse native Windows; warn about slow Windows mounts under WSL."""

    if platform == "win32":
        return UNSUPPORTED_PLATFORM
    if root.as_posix().startswith("/mnt/"):
        return (
            f"{root} is on a Windows drive. Under WSL2, clone the project under ~/ "
            "for speed and working symlinks."
        )
    return None


_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _interrupt(signum, _frame) -> None:
    stop_commands(signum)
    raise Interrupted(signum)


def _interruptible(work) -> int:
    """Run ``work``; on SIGTERM/SIGINT kill its tests and remove its overlays.

    Running mutants are restored by the worker threads as their killed
    commands return. Files whose mutants did not all finish get no snapshot.
    """

    if threading.current_thread() is not threading.main_thread():
        return work()
    previous = {signum: signal.signal(signum, _interrupt) for signum in _STOP_SIGNALS}
    try:
        return work()
    except Interrupted as stop:
        for signum in _STOP_SIGNALS:
            signal.signal(signum, signal.SIG_IGN)
        discard_overlays()
        print(f"Interrupted by {signal.Signals(stop.signum).name}; "
              "unfinished files keep their previous snapshots.", file=sys.stderr)
        return 128 + stop.signum
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    if options.action == "help":
        return _print_help(options)
    missing = _require_crapper()
    if missing:
        print(missing, file=sys.stderr)
        return 2
    root = options.project_root.resolve()
    warning = platform_warning(sys.platform, root)
    if warning:
        print(warning, file=sys.stderr)
    if sys.platform == "win32":
        return 1
    files = select_files(options)
    if not files:
        print("No source files to mutate.")
        return 0
    if not options.scan:
        _restore(root)
        from mutator.engine import needs_mutation

        if not any(needs_mutation(path, root, mutate_all=options.mutate_all, lines=options.lines)
                   for path in files):
            print("No mutations selected; keeping the previous snapshots.")
            return 0
    timings.reset()
    with timings.measure("coverage"):
        try:
            indexes = _prepare_coverage(options, root, files)
        except ensure_crapper().runners.CoverageError as exc:
            print(exc, file=sys.stderr)
            return 2
    if options.scan:
        return _scan(options, root, files)
    with timings.measure("mutation-run"):
        code = _interruptible(lambda: _mutate_files(options, root, files, indexes))
    if options.timings:
        print(timings.summary(), file=sys.stderr, end="")
    return code


def main(argv: list[str] | None = None) -> None:
    sys.exit(run(argv))
