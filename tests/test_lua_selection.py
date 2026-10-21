"""Selected Lua specs retain full-suite safety and pristine worker validation."""

from threading import Event, Lock
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
import shutil

import pytest

from mutator import engine
from mutator.crapper_link import ensure_crapper
from mutator.engine import mutate_file
from mutator.metrics import snapshot_path
from mutator.runner import CommandResult, CommandRunner
from mutator.runner import test_plan as command_for


SOURCE = """local M = {}
function M.add(x)
  return x + 1
end
function M.double(x)
  return x * 2
end
return M
"""
COMMAND = ["lua54", "-e", "run_busted()", "--", "busted", "--ignore-lua", "--run", "unit"]


class Index:
    fresh = False

    def __init__(self, directory, selections=None):
        self.directory = directory
        self.command = list(COMMAND)
        self.specs = {"spec/add_spec.lua": .3, "spec/double unit_spec.lua": .7}
        self.selections = selections if selections is not None else {
            3: ["spec/add_spec.lua"], 6: ["spec/double unit_spec.lua"]
        }

    def select(self, source, line):
        assert source.name == "calc.lua"
        return self.selections.get(line)

    def seconds_for(self, specs):
        return sum(self.specs[spec] for spec in specs)


class RecordingRunner:
    verbose = False

    def __init__(self, relative="src/calc.lua", original=SOURCE):
        self.relative = relative
        self.original = original
        self.calls = []
        self.lock = Lock()

    def run(self, command, cwd, timeout):
        text = (cwd / self.relative).read_text(encoding="utf-8")
        with self.lock:
            self.calls.append((list(command) if isinstance(command, list) else command, cwd, timeout, text))
        seconds = 1.5 if command == COMMAND else .1
        return CommandResult(int(text != self.original), False, seconds, "")


def project(root):
    source = root / "src" / "calc.lua"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    return source


def mutate(source, root, runner, **kwargs):
    options = dict(
        runner=runner, covered_lines={3, 6}, ignore_coverage=False,
        mutate_all=True, lines=None, test_command=None, timeout_factor=10,
        mutation_warning=100, baselines={}, max_workers=2, select_specs=True,
    )
    options.update(kwargs)
    return mutate_file(source, root, **options)


def test_each_mutant_gets_relative_specs_task_and_duration_timeout(tmp_path, monkeypatch):
    source = project(tmp_path)
    index = Index(tmp_path)
    runner = RecordingRunner()
    monkeypatch.setattr(engine, "test_plan", lambda *args: pytest.fail("reuse the index plan"))
    monkeypatch.setattr("os.cpu_count", lambda: 2)
    result = mutate(source, tmp_path, runner, lua_coverage=index, busted_task="unit")
    assert not result.baseline_failed
    assert set(result.statuses.values()) == {"killed"}
    assert runner.calls[0][:3] == (COMMAND, tmp_path, None)
    pristine = [call for call in runner.calls if call[2] is None]
    assert len(pristine) == 3
    assert {tuple(call[0]) for call in pristine} == {
        tuple(COMMAND), tuple([*COMMAND, "--no-keep-going", "spec/add_spec.lua"]),
        tuple([*COMMAND, "--no-keep-going", "spec/double unit_spec.lua"]),
    }
    union_command = [*COMMAND, "--no-keep-going", "spec/add_spec.lua", "spec/double unit_spec.lua"]
    checks = [call for call in runner.calls if call[3] == SOURCE and call[2] is not None]
    assert checks
    assert all(call[0] != union_command for call in checks)
    for command, _, timeout, _ in checks:
        assert timeout == (3 if command[-1] == "spec/add_spec.lua" else 7)
    mutations = [call for call in runner.calls if call[3] != SOURCE]
    assert len(mutations) == len(result.sites)
    for command, _, timeout, text in mutations:
        if text.splitlines()[2] != SOURCE.splitlines()[2]:
            assert command == [*COMMAND, "--no-keep-going", "spec/add_spec.lua"]
            assert timeout == 3
        else:
            assert command == [*COMMAND, "--no-keep-going", "spec/double unit_spec.lua"]
            assert timeout == 7
    assert index.command == COMMAND
    assert source.read_text(encoding="utf-8") == SOURCE


@pytest.mark.parametrize("unknown", [None, []])
def test_unknown_or_empty_selection_checks_full_suite_and_uses_baseline_timeout(tmp_path, unknown):
    source = project(tmp_path)
    runner = RecordingRunner()
    index = Index(tmp_path, {3: ["spec/add_spec.lua"], 6: unknown})
    result = mutate(source, tmp_path, runner, lua_coverage=index)
    assert not result.baseline_failed
    checks = [call for call in runner.calls if call[3] == SOURCE and call[0] == [*COMMAND, "--no-keep-going"]]
    assert checks and all(timeout == 15 for _, _, timeout, _ in checks)
    fallback = [call for call in runner.calls if call[3].splitlines()[5] != SOURCE.splitlines()[5]]
    assert fallback and all(command == [*COMMAND, "--no-keep-going"] and timeout == 15
                            for command, _, timeout, _ in fallback)


def test_selected_timeout_has_two_second_floor(tmp_path):
    source = project(tmp_path)
    runner = RecordingRunner()
    mutate(source, tmp_path, runner, lua_coverage=Index(tmp_path), timeout_factor=.1)
    assert all(timeout == 2 for _, _, timeout, _ in runner.calls if timeout is not None)


def test_full_suite_mode_only_adds_fail_fast_to_mutants(tmp_path, monkeypatch):
    source = project(tmp_path)
    monkeypatch.setattr(engine, "test_plan", lambda *args: (list(COMMAND), tmp_path))
    runner = RecordingRunner()
    result = mutate(source, tmp_path, runner)
    assert not result.baseline_failed
    for command, _, _, text in runner.calls:
        assert command == (COMMAND if text == SOURCE else [*COMMAND, "--no-keep-going"])


def test_custom_command_bypasses_selection_and_busted_flags(tmp_path, monkeypatch):
    source = project(tmp_path)
    index = Index(tmp_path)
    monkeypatch.setattr(index, "select", lambda *args: pytest.fail("custom commands are opaque"))
    runner = RecordingRunner()
    result = mutate(source, tmp_path, runner, test_command="custom test --flag", lua_coverage=index)
    assert not result.baseline_failed
    assert all(command == "custom test --flag" for command, _, _, _ in runner.calls)


def test_full_baseline_is_cached_across_files_with_separate_worker_checks(tmp_path):
    source = project(tmp_path)
    other = tmp_path / "other" / "calc.lua"
    other.parent.mkdir()
    other.write_text(SOURCE, encoding="utf-8")
    index = Index(tmp_path)
    baselines = {}
    first = RecordingRunner()
    second = RecordingRunner("other/calc.lua")
    mutate(source, tmp_path, first, baselines=baselines, lua_coverage=index)
    mutate(other, tmp_path, second, baselines=baselines, lua_coverage=index)
    assert sum(timeout is None for _, _, timeout, _ in first.calls + second.calls) == 3
    assert all(timeout is not None for _, _, timeout, _ in second.calls)
    assert any(text == SOURCE for _, _, _, text in second.calls)


def test_every_worker_must_pass_initial_plan_before_any_mutation(tmp_path, monkeypatch):
    source = project(tmp_path)
    index = Index(tmp_path)
    monkeypatch.setattr("os.cpu_count", lambda: 2)
    mutate(source, tmp_path, RecordingRunner(), lua_coverage=index)
    snapshot = snapshot_path(tmp_path, "calc")
    previous = snapshot.read_bytes()

    class PoisonedWorker(RecordingRunner):
        def run(self, command, cwd, timeout):
            result = super().run(command, cwd, timeout)
            assert (cwd / "src" / "calc.lua").read_text(encoding="utf-8") == SOURCE, "mutated before all checks passed"
            if cwd.name == "worker-1":
                return CommandResult(1, False, .01, "selected-spec setup poisoned")
            return result

    runner = PoisonedWorker()
    result = mutate(source, tmp_path, runner, lua_coverage=index)
    assert result.baseline_failed
    assert "selected-spec setup poisoned" in result.baseline_message
    assert {cwd.name for _, cwd, timeout, _ in runner.calls if timeout is not None} == {"worker-0", "worker-1"}
    assert snapshot.read_bytes() == previous
    assert source.read_text(encoding="utf-8") == SOURCE
    assert not list((tmp_path / "target" / "mutation-workers").iterdir())


@pytest.mark.parametrize("initial_failure", [False, True])
def test_reserved_first_plan_keeps_global_pristine_barrier(tmp_path, monkeypatch, initial_failure):
    from mutator.functions import sites_in_file
    from mutator.workers import WorkerBaselineError, run_mutants

    source = project(tmp_path)
    found = sites_in_file(source.read_bytes().decode("utf-8"), source, tmp_path, "src/calc.lua")
    add = next(site for site in found if site.line == 3)
    double = next(site for site in found if site.line == 6)
    sites = [add, double, replace(add, mutation_id=add.mutation_id + "-repeat")]
    add_command = [*COMMAND, "--no-keep-going", "spec/add_spec.lua"]
    double_command = [*COMMAND, "--no-keep-going", "spec/double unit_spec.lua"]
    plans = {site.mutation_id: (add_command if site.line == 3 else double_command,
                               3 if site.line == 3 else 7) for site in sites}
    entered = [Event(), Event()]
    release = Event()
    mutated_event = Event()
    started = set()
    calls = []
    lock = Lock()
    monkeypatch.setattr("os.cpu_count", lambda: 2)

    class BarrierRunner:
        verbose = False

        def run(self, command, cwd, timeout):
            worker = int(cwd.name.removeprefix("worker-"))
            text = (cwd / "src/calc.lua").read_text(encoding="utf-8")
            with lock:
                first = worker not in started
                started.add(worker)
                calls.append((worker, command, timeout, text))
            if first:
                assert text == SOURCE
                assert command == (add_command if worker == 0 else double_command)
                assert timeout == (3 if worker == 0 else 7)
                entered[worker].set()
                assert release.wait(10), "initial baseline was not released"
                if worker == 1:
                    assert not mutated_event.wait(.1), "mutation began before every pristine check passed"
                return CommandResult(int(initial_failure and worker == 1), False, .01, "initial exact plan failed")
            if text != SOURCE:
                mutated_event.set()
            assert all(event.is_set() for event in entered) and release.is_set()
            return CommandResult(0, False, .01, "")

    outcomes = {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_mutants, tmp_path, source, source.read_bytes(), sites, 2,
            BarrierRunner(), COMMAND, tmp_path, 15, "src/calc.lua", outcomes,
            plans=plans, check_command=[*COMMAND, "union must not execute"], check_timeout=99,
        )
        try:
            assert all(event.wait(10) for event in entered)
            assert not future.done()
            assert len(calls) == 2 and all(text == SOURCE for _, _, _, text in calls)
        finally:
            release.set()
        if initial_failure:
            with pytest.raises(WorkerBaselineError, match="initial exact plan failed"):
                future.result(timeout=10)
        else:
            future.result(timeout=10)
    if initial_failure:
        assert outcomes == {}
        assert len(calls) == 2
    else:
        assert set(outcomes.values()) == {"survived"}
        for worker, expected_line in [(0, 2), (1, 5)]:
            mutated = [call for call in calls if call[0] == worker and call[3] != SOURCE]
            assert mutated and mutated[0][3].splitlines()[expected_line] != SOURCE.splitlines()[expected_line]
        # Even a repeated mutation command gets only one pristine check per worker.
        checks = [(worker, tuple(command)) for worker, command, _, text in calls if text == SOURCE]
        assert len(checks) == len(set(checks))
    assert source.read_text(encoding="utf-8") == SOURCE
    assert not list((tmp_path / "target/mutation-workers").iterdir())


def test_reserved_plan_eliminates_redundant_union_launch(tmp_path):
    from mutator.functions import sites_in_file
    from mutator.workers import run_mutants

    source = project(tmp_path)
    original = source.read_bytes()
    sites = sites_in_file(original.decode("utf-8"), source, tmp_path, "src/calc.lua")
    exact = [*COMMAND, "--no-keep-going", "spec/add_spec.lua"]
    union = [*exact, "spec/double unit_spec.lua"]
    runner = RecordingRunner()
    outcomes = {}
    run_mutants(
        tmp_path, source, original, sites, 1, runner, COMMAND, tmp_path, 15,
        "src/calc.lua", outcomes,
        plans={site.mutation_id: (exact, 3) for site in sites},
        check_command=union, check_timeout=10,
    )
    # One exact pristine launch, then one launch per mutant; no union launch.
    assert len(runner.calls) == 1 + len(sites)
    assert sum(command == union for command, _, _, _ in runner.calls) == 0
    assert sum(text == SOURCE for _, _, _, text in runner.calls) == 1
    assert all(command == exact and timeout == 3 for command, _, timeout, _ in runner.calls)
    assert outcomes and set(outcomes.values()) == {"killed"}
    assert source.read_bytes() == original


def test_selection_does_not_run_uncovered_sites(tmp_path):
    source = project(tmp_path)
    index = Index(tmp_path)
    runner = RecordingRunner()
    result = mutate(source, tmp_path, runner, covered_lines={3}, lua_coverage=index)
    assert any(status == "uncovered" for status in result.statuses.values())
    assert all("spec/double unit_spec.lua" not in command for command, _, _, _ in runner.calls)
    assert all(text.splitlines()[5] == SOURCE.splitlines()[5] for _, _, _, text in runner.calls)


def test_workers_accept_result_double_without_timing(tmp_path):
    from mutator.workers import run_mutants
    from mutator.functions import sites_in_file

    source = project(tmp_path)
    site = sites_in_file(SOURCE, source, tmp_path, "src/calc.lua")[0]

    class Untimed:
        verbose = False

        def run(self, command, cwd, timeout):
            return SimpleNamespace(code=0, timed_out=False, output="", launch_error=None)

    outcomes = {}
    run_mutants(tmp_path, source, source.read_bytes(), [site], 1, Untimed(),
                COMMAND, tmp_path, 2, "src/calc.lua", outcomes,
                plans={site.mutation_id: ([*COMMAND, "spec/add_spec.lua"], 2)})
    assert outcomes == {site.mutation_id: "survived"}


def test_failed_original_full_baseline_prevents_selected_execution(tmp_path):
    source = project(tmp_path)

    class FailedFull(RecordingRunner):
        def run(self, command, cwd, timeout):
            super().run(command, cwd, timeout)
            return CommandResult(1, False, .1, "unrelated spec failed")

    runner = FailedFull()
    result = mutate(source, tmp_path, runner, lua_coverage=Index(tmp_path))
    assert result.baseline_failed
    assert "unrelated spec failed" in result.baseline_message
    assert len(runner.calls) == 1 and runner.calls[0][0] == COMMAND
    assert not snapshot_path(tmp_path, "calc").exists()


def test_selected_survivors_are_executed_again_in_differential_run(tmp_path):
    source = project(tmp_path)

    class Surviving(RecordingRunner):
        def run(self, command, cwd, timeout):
            super().run(command, cwd, timeout)
            return CommandResult(0, False, .01, "")

    runner = Surviving()
    index = Index(tmp_path)
    first = mutate(source, tmp_path, runner, lua_coverage=index)
    count = sum(text != SOURCE for _, _, _, text in runner.calls)
    second = mutate(source, tmp_path, runner, lua_coverage=index, mutate_all=False)
    assert count > 0
    assert sum(text != SOURCE for _, _, _, text in runner.calls) == 2 * count
    assert first.statuses == second.statuses
    assert set(second.statuses.values()) == {"survived"}


def test_non_lua_sources_ignore_lua_selection(tmp_path, monkeypatch):
    source = tmp_path / "src" / "demo.py"
    source.parent.mkdir()
    original = "def add(x):\n    return x + 1\n"
    source.write_text(original, encoding="utf-8")
    index = Index(tmp_path)
    monkeypatch.setattr(index, "select", lambda *args: pytest.fail("Lua index used for Python"))
    monkeypatch.setattr(engine, "test_plan", lambda *args: (["python", "check.py"], tmp_path))
    runner = RecordingRunner("src/demo.py", original)
    result = mutate(source, tmp_path, runner, ignore_coverage=True, lua_coverage=index)
    assert not result.baseline_failed
    assert all(command == ["python", "check.py"] for command, _, _, _ in runner.calls)


def test_failing_exact_subset_falls_back_despite_passing_union(tmp_path):
    source = project(tmp_path)
    index = Index(tmp_path)
    failed_command = [*COMMAND, "--no-keep-going", "spec/double unit_spec.lua"]

    class DependentSubset(RecordingRunner):
        def run(self, command, cwd, timeout):
            super().run(command, cwd, timeout)
            # The combined suite supplies state that double's spec needs.
            return CommandResult(int(command == failed_command), False, .1, "missing shared state")

    runner = DependentSubset()
    baselines = {}
    result = mutate(source, tmp_path, runner, lua_coverage=index, baselines=baselines)
    assert not result.baseline_failed
    assert set(result.statuses.values()) == {"survived"}
    pristine = [call for call in runner.calls if call[2] is None]
    assert sum(command == failed_command for command, _, _, _ in pristine) == 1
    checks = [call for call in runner.calls if call[2] is not None and call[3] == SOURCE and call[0] == [*COMMAND, "--no-keep-going"]]
    assert checks
    for command, _, timeout, text in runner.calls:
        if text.splitlines()[5] != SOURCE.splitlines()[5]:
            assert command == [*COMMAND, "--no-keep-going"]
            assert timeout == 2
    before = len(runner.calls)
    mutate(source, tmp_path, runner, lua_coverage=index, baselines=baselines)
    assert all(timeout is not None for _, _, timeout, _ in runner.calls[before:])


def test_exact_pristine_duration_prevents_startup_timeout_false_kills(tmp_path):
    source = project(tmp_path)

    class SlowStartup(RecordingRunner):
        def run(self, command, cwd, timeout):
            super().run(command, cwd, timeout)
            timed_out = timeout is not None and timeout < 25
            return CommandResult(124 if timed_out else 0, timed_out, 2.5, "")

    runner = SlowStartup()
    result = mutate(source, tmp_path, runner, lua_coverage=Index(tmp_path))
    assert not result.baseline_failed
    assert set(result.statuses.values()) == {"survived"}
    assert all(timeout >= 25 for _, _, timeout, text in runner.calls if text != SOURCE)
    checks = [timeout for _, _, timeout, text in runner.calls if timeout is not None and text == SOURCE]
    assert checks and all(timeout >= 25 for timeout in checks)


@pytest.mark.parametrize("change", ["remove", "add", "edit"])
def test_preflight_requires_entire_function_digest_set_unchanged(tmp_path, change):
    source = project(tmp_path)
    extra = "\nfunction M.label(x)\n  return x\nend\n"
    extended = SOURCE.replace("return M\n", extra + "return M\n")
    if change in {"remove", "edit"}:
        source.write_text(extended, encoding="utf-8")
    original = source.read_text(encoding="utf-8")
    mutate(source, tmp_path, RecordingRunner(original=original), lua_coverage=Index(tmp_path))
    assert not engine.needs_mutation(source, tmp_path)
    if change == "remove":
        updated = SOURCE
    elif change == "add":
        updated = extended
    else:
        updated = extended.replace("return x\n", "return tostring(x)\n")
    source.write_text(updated, encoding="utf-8")
    assert engine.needs_mutation(source, tmp_path)


def test_exact_worker_subset_failure_preserves_snapshot_and_has_no_outcomes(tmp_path, monkeypatch):
    source = project(tmp_path)
    index = Index(tmp_path)

    class Surviving(RecordingRunner):
        def run(self, command, cwd, timeout):
            super().run(command, cwd, timeout)
            return CommandResult(0, False, .1, "")

    mutate(source, tmp_path, Surviving(), lua_coverage=index)
    snapshot = snapshot_path(tmp_path, "calc")
    previous = snapshot.read_bytes()
    captured = {}
    real_run_mutants = engine.run_mutants

    def capture(*args, **kwargs):
        captured["outcomes"] = args[10]
        return real_run_mutants(*args, **kwargs)

    monkeypatch.setattr(engine, "run_mutants", capture)

    class OverlaySubsetFailure(RecordingRunner):
        def run(self, command, cwd, timeout):
            result = super().run(command, cwd, timeout)
            if cwd != tmp_path and command == [*COMMAND, "--no-keep-going", "spec/double unit_spec.lua"]:
                assert (cwd / "src/calc.lua").read_text(encoding="utf-8") == SOURCE
                return CommandResult(1, False, .1, "overlay subset setup failed")
            return result

    runner = OverlaySubsetFailure()
    result = mutate(source, tmp_path, runner, lua_coverage=index, max_workers=1)
    assert result.baseline_failed
    assert "overlay subset setup failed" in result.baseline_message
    assert result.statuses == {}
    # A completed earlier kill is discarded when a later pristine subset fails.
    assert captured["outcomes"]
    assert all(status == "survived" for status in captured["outcomes"].values())
    assert snapshot.read_bytes() == previous
    assert source.read_text(encoding="utf-8") == SOURCE
    worker_pristine = [command for command, cwd, _, text in runner.calls if cwd != tmp_path and text == SOURCE]
    assert worker_pristine.count([*COMMAND, "--no-keep-going", "spec/add_spec.lua"]) == 1
    assert worker_pristine.count([*COMMAND, "--no-keep-going", "spec/double unit_spec.lua"]) == 1


@pytest.mark.parametrize("confirmation_failure", [False, True])
def test_selected_survivors_require_full_confirmation_and_pristine_baseline(tmp_path, confirmation_failure):
    from mutator import timings

    source = project(tmp_path)
    index = Index(tmp_path)
    mutate(source, tmp_path, RecordingRunner(), lua_coverage=index)
    snapshot = snapshot_path(tmp_path, "calc")
    previous = snapshot.read_bytes()
    full = [*COMMAND, "--no-keep-going"]

    class DownstreamAssertion(RecordingRunner):
        def run(self, command, cwd, timeout):
            super().run(command, cwd, timeout)
            text = (cwd / "src/calc.lua").read_text(encoding="utf-8")
            # Selected tests compute the result; a different spec asserts it.
            failed = command == full and (text != SOURCE or confirmation_failure)
            return CommandResult(int(failed), False, .1, "full worker setup failed" if text == SOURCE else "assertion failed")

    runner = DownstreamAssertion()
    baselines = {}
    timeout_factor = 10
    timings.reset()
    result = mutate(source, tmp_path, runner, lua_coverage=index,
                    baselines=baselines, timeout_factor=timeout_factor)
    pristine_seconds = baselines[(engine._command_key(COMMAND), str(tmp_path))][1]
    expected_timeout = max(2.0, pristine_seconds * timeout_factor)
    full_pristine = [call for call in runner.calls if call[0] == full and call[3] == SOURCE]
    assert len(full_pristine) == 1
    confirmations = [call for call in runner.calls if call[0] == full and call[3] != SOURCE]
    if confirmation_failure:
        assert result.baseline_failed
        assert "full worker setup failed" in result.baseline_message
        assert not confirmations
        assert snapshot.read_bytes() == previous
    else:
        assert not result.baseline_failed
        assert set(result.statuses.values()) == {"killed"}
        assert len(confirmations) == len(result.sites)
        assert all(call[2] == expected_timeout for call in confirmations)
        assert "timing survivor-confirmation:" in timings.summary()
        assert f"{len(result.sites)} run(s)" in timings.summary()
    assert source.read_text(encoding="utf-8") == SOURCE


@pytest.mark.parametrize("cross_dependency", [False, True])
def test_real_busted_selected_specs_match_full_suite(tmp_path, cross_dependency):
    if shutil.which("busted") is None or ensure_crapper().runners.lua_interpreter() is None:
        pytest.skip("Busted and Lua 5.4 are required")
    source = project(tmp_path)
    (tmp_path / ".busted").write_text(
        "return {default = {lpath = 'src/?.lua;' .. package.path}}\n", encoding="utf-8")
    specs = tmp_path / "spec"
    specs.mkdir()
    (specs / "add_spec.lua").write_text(
        "debug.getregistry().shared_calc_ready = true\n"
        "local calc = require('calc')\n"
        "describe('add', function() it('adds', function() assert.equal(3, calc.add(2)) end) end)\n",
        encoding="utf-8")
    (specs / "double unit_spec.lua").write_text(
        ("assert(debug.getregistry().shared_calc_ready, 'shared state required')\n" if cross_dependency else "")
        + "local calc = require('calc')\n"
        "describe('double', function() it('doubles', function() assert.equal(6, calc.double(3)) end) end)\n",
        encoding="utf-8")
    command, cwd = command_for(tmp_path, source, "lua", None)
    index = Index(cwd)
    index.command = command
    runner = CommandRunner()
    full = mutate(source, tmp_path, runner, max_workers=2)
    assert not full.baseline_failed, full.baseline_message
    selected = mutate(source, tmp_path, runner, lua_coverage=index, max_workers=2)
    assert not selected.baseline_failed, selected.baseline_message
    assert selected.statuses == full.statuses
    assert selected.forms == full.forms
    assert set(selected.statuses.values()) == {"killed"}
    assert source.read_text(encoding="utf-8") == SOURCE
