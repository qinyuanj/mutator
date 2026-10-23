"""Apply each selected mutant, run tests, and record the snapshot."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mutator.lua_coverage import LuaCoverageIndex

from mutator.crapper_link import ensure_crapper
from mutator.functions import form_digests, form_spans, sites_in_file
from mutator.metrics import load_history, source_key, write_results
from mutator.model import FormResult, RunResult, Site
from mutator.report import format_scan
from mutator.runner import Command, CommandRunner, display_command, run_tests, test_plan
from mutator.workers import (
    QueuedFile, WorkerBaselineError, WorkerPool, WorkerSlots, run_mutants, run_queue,
)
from mutator.timings import measure


def restore_backups(root: Path) -> list[Path]:
    """Put back sources left mutated by a killed run."""

    base = root / "target" / "mutator-backup"
    restored: list[Path] = []
    if not base.is_dir():
        return restored
    for backup in sorted(path for path in base.rglob("*") if path.is_file()):
        relative = backup.relative_to(base)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(backup.read_bytes())
        backup.unlink()
        restored.append(target)
    return restored


def _drop_bytecode(path: Path) -> None:
    """A same-second .pyc would hide the mutant from Python."""

    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return
    for item in cache.glob(path.stem + ".*.pyc"):
        item.unlink(missing_ok=True)


def _backup(root: Path, path: Path, original: bytes) -> Path:
    relative = path.resolve().relative_to(root.resolve())
    backup = root / "target" / "mutator-backup" / relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        backup.write_bytes(original)
    return backup


def _covered(site: Site, lines: set[int] | None, ignore_coverage: bool) -> bool:
    if ignore_coverage:
        return True
    if lines is None:
        return False
    return site.line in lines


def _select(
    sites: list[Site],
    covered: dict[str, bool],
    history,
    digests: dict[tuple[str, str], str],
    mutate_all: bool,
    lines: set[int] | None,
) -> list[Site]:
    chosen = []
    for site in sites:
        if not covered[site.mutation_id]:
            continue
        if lines is not None and site.line not in lines:
            continue
        key = (site.namespace, site.form_id)
        prior = history.forms.get(key)
        unchanged = prior is not None and prior.digest == digests.get(key)
        if mutate_all or not unchanged:
            chosen.append(site)
            continue
        if history.outcomes.get(site.mutation_id) == "killed":
            continue
        chosen.append(site)
    return chosen


def _carry_forward(sites: list[Site], covered: dict[str, bool], history, digests) -> dict[str, str]:
    by_id = {site.mutation_id: site for site in sites}
    carried = {}
    for mutation, status in history.outcomes.items():
        site = by_id.get(mutation)
        if site is None or not covered[site.mutation_id]:
            continue
        key = (site.namespace, site.form_id)
        prior = history.forms.get(key)
        if prior is not None and prior.digest == digests.get(key):
            carried[mutation] = status
    return carried


def _forms(
    source: str,
    path: Path,
    root: Path,
    file_key: str,
    sites: list[Site],
    covered: dict[str, bool],
    outcomes: dict[str, str],
    lines: set[int] | None,
) -> list[FormResult]:
    spans = form_spans(source, path, root)
    digests = form_digests(source, path, root)
    grouped: dict[tuple[str, str], list[Site]] = {}
    for site in sites:
        grouped.setdefault((site.namespace, site.form_id), []).append(site)
    forms = []
    for key, (start, end, name, private) in sorted(spans.items(), key=lambda item: item[1][0]):
        namespace, form = key
        owned = grouped.get(key, [])
        killed = survived = uncovered = 0
        for site in owned:
            if not covered[site.mutation_id]:
                uncovered += 1
                continue
            status = outcomes.get(site.mutation_id)
            if status == "killed":
                killed += 1
            elif status == "survived":
                survived += 1
            elif lines is None:
                survived += 1
        forms.append(
            FormResult(
                id=form,
                namespace=namespace,
                name=name,
                private=private,
                file=file_key,
                line=start,
                end_line=end,
                digest=digests[key],
                killed=killed,
                survived=survived,
                uncovered=uncovered,
                sites=len(owned),
            )
        )
    return forms


def _decode(path: Path) -> tuple[bytes, str] | None:
    original = path.read_bytes()
    try:
        return original, original.decode("utf-8")
    except UnicodeError:
        return None


def _coverage_map(
    found: list[Site], covered_lines: set[int] | None, ignore_coverage: bool
) -> dict[str, bool]:
    return {site.mutation_id: _covered(site, covered_lines, ignore_coverage) for site in found}


def _warn_uncovered(file_key: str, found, covered_lines, ignore_coverage: bool) -> None:
    if covered_lines is not None or ignore_coverage or not found:
        return
    print(
        f"No coverage data for {file_key}; its sites are uncovered. "
        "Pass --no-coverage to run them anyway.",
        file=sys.stderr,
    )


def _warn_many(file_key: str, selected, mutation_warning: int) -> None:
    if len(selected) > mutation_warning:
        print(
            f"WARNING: {len(selected)} covered mutations selected in {file_key}.",
            file=sys.stderr,
        )


def _command_key(command: Command) -> tuple[str, ...]:
    """A hashable identity. A list cannot be a dict key."""

    if isinstance(command, str):
        return ("sh", command)
    return ("argv", *command)


_NO_TESTS = re.compile(r"^Ran 0 tests in ", re.MULTILINE)


def _remember_baseline(baselines, cache_key, runner, command: Command, cwd: Path) -> None:
    if cache_key in baselines:
        return
    with measure("baseline"):
        baseline = run_tests(runner, command, cwd, None)
    # unittest before 3.12 exits 0 when discovery finds nothing. Every mutant
    # would then survive a suite that tests nothing.
    if baseline.code != 0 or _NO_TESTS.search(baseline.output):
        tail = "\n".join(baseline.output.splitlines()[-20:])
        baselines[cache_key] = (False, baseline.seconds, tail)
        return
    baselines[cache_key] = (True, baseline.seconds, "")


def _baseline_failure(file_key: str, command: Command, tail: str) -> RunResult:
    message = f"Baseline failed for {file_key}: {display_command(command)}"
    if tail:
        message = f"{message}\n{tail}"
    return RunResult(path=file_key, forms=[], written=[], baseline_failed=True, baseline_message=message)


def _lua_plans(
    index: LuaCoverageIndex,
    path: Path,
    selected: list[Site],
    command: list[str],
    timeout: float,
    timeout_factor: float,
    runner,
    baselines,
) -> tuple[dict[str, tuple[Command, float]], Command, float]:
    plans = {}
    subsets: dict[tuple[str, ...], tuple[Command, float] | None] = {}
    union: set[str] = set()
    full_suite = False
    for site in selected:
        specs = index.select(path, site.line)
        key = tuple(specs or ())
        if key and key not in subsets:
            selected_command = [*command, *specs]
            cache_key = (_command_key(selected_command), str(index.directory))
            _remember_baseline(baselines, cache_key, runner, selected_command, index.directory)
            ok, seconds, _tail = baselines[cache_key]
            subsets[key] = (
                selected_command,
                max(2.0, index.seconds_for(specs) * timeout_factor, seconds * timeout_factor),
            ) if ok else None
        plan = subsets.get(key)
        if plan is None:
            plans[site.mutation_id] = (command, timeout)
            full_suite = True
            continue
        union.update(specs)
        plans[site.mutation_id] = plan
    if full_suite:
        return plans, index.command, timeout
    specs = sorted(union)
    check_timeout = max(
        2.0, index.seconds_for(specs) * timeout_factor,
        sum(baselines[(_command_key(plan[0]), str(index.directory))][1]
            for plan in subsets.values() if plan is not None) * timeout_factor,
    )
    return plans, [*command, *specs], check_timeout


def _covering_first(index: LuaCoverageIndex, path: Path, selected: list[Site]) -> list[str]:
    """Every spec of the suite, the ones covering this file's mutants first.

    The full suite still runs, so outcomes keep their full-suite meaning; a
    killing spec just runs early under --no-keep-going
    (docs/adr/0003-covering-specs-first.md).
    """

    covering: set[str] = set()
    for site in selected:
        covering.update(index.select(path, site.line) or ())
    return [*sorted(covering), *(spec for spec in sorted(index.specs) if spec not in covering)]


def _baseline_plan(
    path, root, runner, language, test_command, busted_task, baselines, timeout_factor,
    file_key, lua_coverage=None,
) -> tuple[Command, Path, float] | RunResult:
    """The file's baseline command, its cwd and the mutant timeout, or the baseline failure."""

    standard_lua = language == "lua" and not test_command
    if standard_lua and lua_coverage is not None:
        command, cwd = lua_coverage.command, lua_coverage.directory
    else:
        command, cwd = test_plan(root, path, language, test_command, busted_task)
    cache_key = (_command_key(command), str(cwd))
    if standard_lua and lua_coverage is not None and getattr(lua_coverage, "fresh", False):
        # The fresh coverage run already proved the pristine suite passes, so
        # the baseline does not run again. Its coverage-inflated duration
        # becomes the timeout base (docs/adr/0001-coverage-run-as-baseline.md).
        baselines.setdefault(cache_key, (True, lua_coverage.seconds, ""))
    _remember_baseline(baselines, cache_key, runner, command, cwd)
    ok, seconds, tail = baselines[cache_key]
    if not ok:
        return _baseline_failure(file_key, command, tail)
    return command, cwd, max(2.0, seconds * timeout_factor)


def _full_suite_command(command: list[str], lua_coverage, path: Path, selected: list[Site]) -> list[str]:
    command = [*command, "--no-keep-going"]
    if lua_coverage is None:
        return command
    return [*command, *_covering_first(lua_coverage, path, selected)]


def _apply_selected(
    path,
    root,
    original,
    selected,
    runner,
    language,
    test_command,
    timeout_factor,
    file_key,
    baselines,
    outcomes,
    max_workers,
    busted_task,
    lua_coverage=None,
    worker_pool=None,
    select_specs: bool = False,
) -> RunResult | None:
    if not selected:
        return None
    standard_lua = language == "lua" and not test_command
    plan = _baseline_plan(path, root, runner, language, test_command, busted_task, baselines,
                   timeout_factor, file_key, lua_coverage)
    if isinstance(plan, RunResult):
        return plan
    command, cwd, timeout = plan
    plans = None
    check_command, check_timeout = command, timeout
    if standard_lua:
        if select_specs and lua_coverage is not None:
            command = [*command, "--no-keep-going"]
            plans, check_command, check_timeout = _lua_plans(
                lua_coverage, path, selected, command, timeout, timeout_factor, runner, baselines
            )
        else:
            command = _full_suite_command(command, lua_coverage, path, selected)
    confirmation = (
        (command, timeout) if standard_lua and select_specs and lua_coverage is not None else None
    )
    try:
        run_mutants(
            root,
            path,
            original,
            selected,
            max_workers,
            runner,
            command,
            cwd,
            timeout,
            file_key,
            outcomes,
            plans=plans,
            check_command=check_command,
            check_timeout=check_timeout,
            confirmation=confirmation,
            pool=worker_pool if standard_lua or language == "rust" else None,
        )
    except WorkerBaselineError as error:
        return _baseline_failure(file_key, command, str(error))
    return None


def _statuses(found: list[Site], covered: dict[str, bool], outcomes: dict[str, str]) -> dict[str, str]:
    statuses = {}
    for site in found:
        if not covered[site.mutation_id]:
            statuses[site.mutation_id] = "uncovered"
        elif site.mutation_id in outcomes:
            statuses[site.mutation_id] = outcomes[site.mutation_id]
    return statuses


def _restore_if_dirty(path: Path, original: bytes) -> None:
    if path.read_bytes() != original:
        path.write_bytes(original)


@dataclass
class _Prepared:
    """A file whose sites are selected and whose snapshot is still unwritten."""

    path: Path
    root: Path
    language: str
    file_key: str
    original: bytes
    source: str
    found: list[Site]
    covered: dict[str, bool]
    lines: set[int] | None
    selected: list[Site]
    outcomes: dict[str, str]


def _prepare(
    path: Path,
    root: Path,
    covered_lines: set[int] | None,
    ignore_coverage: bool,
    mutate_all: bool,
    lines: set[int] | None,
    test_command: str | None,
    mutation_warning: int,
) -> _Prepared | RunResult:
    root = root.resolve()
    path = path.resolve()
    crapper = ensure_crapper()
    language = crapper.discover.language_of(path)
    file_key = source_key(path, root)
    if language is None:
        return RunResult(path=file_key, forms=[], written=[], skipped="unsupported file")
    decoded = _decode(path)
    if decoded is None:
        return RunResult(path=file_key, forms=[], written=[], skipped="file is not UTF-8")
    original, source = decoded
    found = sites_in_file(source, path, root, file_key)
    covered = _coverage_map(found, covered_lines, ignore_coverage)
    _warn_uncovered(file_key, found, covered_lines, ignore_coverage)
    history = load_history(root, file_key)
    digests = form_digests(source, path, root)
    selected = _select(found, covered, history, digests, mutate_all, lines)
    if language == "lua" and not test_command:
        selected.sort(key=lambda site: history.outcomes.get(site.mutation_id) != "survived")
    outcomes = _carry_forward(found, covered, history, digests)
    _warn_many(file_key, selected, mutation_warning)
    return _Prepared(path, root, language, file_key, original, source, found, covered, lines,
                     selected, outcomes)


def _publish(prepared: _Prepared) -> RunResult:
    p = prepared
    forms = _forms(p.source, p.path, p.root, p.file_key, p.found, p.covered, p.outcomes, p.lines)
    written = write_results(p.root, p.file_key, forms, p.outcomes)
    _restore_if_dirty(p.path, p.original)
    return RunResult(
        path=p.file_key,
        forms=forms,
        written=[item.as_posix() for item in written],
        sites=p.found,
        statuses=_statuses(p.found, p.covered, p.outcomes),
    )


def mutate_file(
    path: Path,
    root: Path,
    *,
    runner: CommandRunner,
    covered_lines: set[int] | None,
    ignore_coverage: bool,
    mutate_all: bool,
    lines: set[int] | None,
    test_command: str | None,
    timeout_factor: float,
    mutation_warning: int,
    baselines: dict[tuple[tuple[str, ...], str], tuple[bool, float, str]],
    max_workers: int | None = None,
    busted_task: str | None = None,
    lua_coverage: LuaCoverageIndex | None = None,
    worker_pool: WorkerPool | WorkerSlots | None = None,
    select_specs: bool = False,
) -> RunResult:
    """Mutate one file and write its namespaces into `.metrics/mutate`.

    ``worker_pool`` is the run's Lua pool or its Rust worker slots.
    """

    prepared = _prepare(path, root, covered_lines, ignore_coverage, mutate_all, lines,
                        test_command, mutation_warning)
    if isinstance(prepared, RunResult):
        return prepared
    failure = _apply_selected(
        prepared.path,
        prepared.root,
        prepared.original,
        prepared.selected,
        runner,
        prepared.language,
        test_command,
        timeout_factor,
        prepared.file_key,
        baselines,
        prepared.outcomes,
        max_workers,
        busted_task,
        lua_coverage,
        worker_pool,
        select_specs,
    )
    if failure is not None:
        return failure
    return _publish(prepared)


def mutate_queued(
    files: list[tuple[Path, set[int] | None, LuaCoverageIndex | None]],
    root: Path,
    *,
    runner: CommandRunner,
    ignore_coverage: bool,
    mutate_all: bool,
    lines: set[int] | None,
    timeout_factor: float,
    mutation_warning: int,
    baselines: dict[tuple[tuple[str, ...], str], tuple[bool, float, str]],
    max_workers: int | None,
    busted_task: str | None,
    worker_pool: WorkerPool,
) -> list[RunResult]:
    """Mutate full-suite Lua files on one shared worker queue.

    Each entry is a file, its covered lines and its spec index. Every file's
    snapshot is written as soon as its own last mutant finishes. A baseline
    failure stays with its file; a worker failure fails the files that had
    not completed and keeps the snapshots of those that had. Results follow
    the order of ``files``.
    """

    results: dict[int, RunResult] = {}
    prepared: dict[int, _Prepared] = {}
    jobs: dict[int, QueuedFile] = {}
    for position, (path, covered_lines, lua_coverage) in enumerate(files):
        item = _prepare(path, root, covered_lines, ignore_coverage, mutate_all, lines, None,
                        mutation_warning)
        if isinstance(item, RunResult):
            results[position] = item
            continue
        if not item.selected:
            results[position] = _publish(item)
            continue
        plan = _baseline_plan(item.path, item.root, runner, item.language, None, busted_task, baselines,
                       timeout_factor, item.file_key, lua_coverage)
        if isinstance(plan, RunResult):
            results[position] = plan
            continue
        command, cwd, timeout = plan
        prepared[position] = item
        jobs[position] = QueuedFile(
            relative=item.path.relative_to(item.root).as_posix(),
            original=item.original,
            sites=item.selected,
            command=_full_suite_command(command, lua_coverage, item.path, item.selected),
            cwd=cwd,
            timeout=timeout,
            file_key=item.file_key,
            check_command=command,
            check_timeout=timeout,
        )
    positions = {id(job): position for position, job in jobs.items()}

    def publish(job: QueuedFile) -> None:
        position = positions[id(job)]
        prepared[position].outcomes.update(job.completed)
        results[position] = _publish(prepared[position])

    try:
        run_queue(worker_pool, list(jobs.values()), max_workers, runner, publish)
    except WorkerBaselineError as error:
        for position, job in jobs.items():
            if position not in results:
                results[position] = _baseline_failure(job.file_key, job.command, str(error))
    return [results[position] for position in range(len(files))]


def needs_mutation(
    path: Path, root: Path, *, mutate_all: bool = False, lines: set[int] | None = None,
) -> bool:
    """Prove an unchanged, fully killed selection needs no coverage or tests.

    Treat every site as covered so a previous coverage gap cannot be silently
    carried forward. Empty files still take the normal snapshot publication path.
    """

    decoded = _decode(path)
    if decoded is None:
        return True
    _original, source = decoded
    file_key = source_key(path, root)
    sites = sites_in_file(source, path, root, file_key)
    history = load_history(root, file_key)
    if not sites or not history.forms:
        return True
    digests = form_digests(source, path, root)
    if digests != {key: prior.digest for key, prior in history.forms.items()}:
        return True
    return bool(_select(sites, {site.mutation_id: True for site in sites},
                        history, digests, mutate_all, lines))


def scan_file(
    path: Path,
    root: Path,
    *,
    covered_lines: set[int] | None,
    ignore_coverage: bool,
    lines: set[int] | None,
) -> str:
    """Inventory mutation sites without running tests or writing metrics."""

    root = root.resolve()
    path = path.resolve()
    file_key = source_key(path, root)
    source = path.read_text(encoding="utf-8")
    found = sites_in_file(source, path, root, file_key)
    if lines is not None:
        found = [site for site in found if site.line in lines]
    covered = None
    if not ignore_coverage and covered_lines is not None:
        covered = {site.mutation_id: site.line in covered_lines for site in found}
    history = load_history(root, file_key)
    digests = form_digests(source, path, root)
    changed = set()
    if history.forms:
        changed = {
            key
            for key, digest in digests.items()
            if key not in history.forms or history.forms[key].digest != digest
        }
    return format_scan(file_key, found, changed, covered)
