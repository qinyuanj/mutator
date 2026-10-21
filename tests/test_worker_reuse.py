"""Cross-file reuse must preserve outcomes and fail closed before publication."""
from pathlib import Path

import pytest

from mutator.model import Site
from mutator.runner import CommandResult
from mutator.workers import WorkerBaselineError, WorkerPool, run_mutants


def fixture(root):
    (root / "src").mkdir()
    sources = [root / "src" / name for name in ("a.lua", "b.lua")]
    for source in sources:
        source.write_bytes(b"return 1\n")
    return sources


def site(source, value):
    return Site(source.name, "value", "defn/value", "value", 1,
                7, 8, "1", str(value), "number", source.name + str(value))


class Runner:
    verbose = False

    def __init__(self):
        self.checks = []
        self.calls = []
        self.corrupt = False
        self.fail = False
        self.remove = None

    def run(self, command, cwd, timeout):
        values = [(cwd / "src" / name).read_bytes() for name in ("a.lua", "b.lua")]
        self.calls.append((command, cwd, values))
        if values == [b"return 1\n", b"return 1\n"]:
            self.checks.append((command, cwd))
            return CommandResult(0, False, .01, "")
        if self.fail:
            raise OSError("runner broke")
        if self.remove:
            (cwd / "src" / "a.lua").unlink()
            if self.remove == "directory":
                (cwd / "src" / "b.lua").unlink()
                (cwd / "src").rmdir()
        if self.corrupt:
            # A mutant can appear killed by corrupting a different private file.
            (cwd / "src" / "b.lua").write_bytes(b"broken\n")
        return CommandResult(17 if b"return 3\n" in values else 0, False, .01, "")


def run(root, source, runner, pool=None, command="suite", outcomes=None):
    outcomes = {} if outcomes is None else outcomes
    run_mutants(root, source, source.read_bytes(), [site(source, 2), site(source, 3)],
                2, runner, command, root, 2, source.name, outcomes, pool=pool)
    return outcomes


def test_real_lua_multifile_reuse_preserves_loader_binding(tmp_path):
    from mutator.crapper_link import ensure_crapper
    from mutator.runner import CommandRunner
    lua = ensure_crapper().runners.lua_interpreter()
    if lua is None:
        pytest.skip("native Lua interpreter unavailable")
    sources = fixture(tmp_path)
    script = (
        "package.path = './src/?.lua;' .. package.path; "
        "local a,b = require('a'),require('b'); "
        "assert((a == 1 or a == 2) and (b == 1 or b == 2))"
    )
    runner = CommandRunner()
    expected = [run(tmp_path, source, runner, command=[lua, "-e", script]) for source in sources]
    with WorkerPool(tmp_path, sources) as pool:
        actual = [run(tmp_path, source, runner, pool, command=[lua, "-e", script]) for source in sources]
    assert actual == expected == [
        {"a.lua2": "survived", "a.lua3": "killed"},
        {"b.lua2": "survived", "b.lua3": "killed"},
    ]
    assert all(source.read_bytes() == b"return 1\n" for source in sources)


def test_multifile_parity_and_checks_amortized(tmp_path, monkeypatch):
    sources = fixture(tmp_path)
    monkeypatch.setattr("os.cpu_count", lambda: 2)
    reference = Runner()
    expected = [run(tmp_path, source, reference) for source in sources]
    runner = Runner()
    with WorkerPool(tmp_path, sources) as pool:
        actual = [run(tmp_path, source, runner, pool) for source in sources]
        assert actual == expected
        assert len(reference.checks) == 4
        assert len(runner.checks) == 2
        assert len({cwd for _, cwd in runner.checks}) == 2
    assert all(source.read_bytes() == b"return 1\n" for source in sources)
    assert not list((tmp_path / "target" / "mutation-workers").iterdir())


def test_new_command_requires_pristine_check(tmp_path):
    sources = fixture(tmp_path)
    runner = Runner()
    with WorkerPool(tmp_path, sources) as pool:
        run(tmp_path, sources[0], runner, pool)
        run(tmp_path, sources[1], runner, pool, command=["different", "suite"])
    assert {str(command) for command, _ in runner.checks} == {"suite", "['different', 'suite']"}


@pytest.mark.parametrize("failure", ["corrupt", "fail"])
def test_failed_worker_publishes_no_outcomes_and_poisoned_pool(tmp_path, failure):
    sources = fixture(tmp_path)
    runner = Runner()
    outcomes = {"history": "killed"}
    with WorkerPool(tmp_path, sources) as pool:
        run(tmp_path, sources[0], runner, pool)
        setattr(runner, failure, True)
        with pytest.raises((WorkerBaselineError, OSError)):
            run(tmp_path, sources[0], runner, pool, outcomes=outcomes)
        assert outcomes == {"history": "killed"}
        assert pool.failed
        with pytest.raises(WorkerBaselineError):
            run(tmp_path, sources[1], Runner(), pool)
    assert all(source.read_bytes() == b"return 1\n" for source in sources)


@pytest.mark.parametrize("failure", ["corrupt", "file", "directory"])
def test_engine_worker_failure_preserves_previous_snapshot(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    from mutator import engine
    from mutator.model import History
    sources = fixture(tmp_path)
    snapshot = tmp_path / ".metrics" / "mutate" / "old.edn"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"previous snapshot\n")
    monkeypatch.setattr(engine, "ensure_crapper", lambda: SimpleNamespace(
        discover=SimpleNamespace(language_of=lambda path: "lua")))
    monkeypatch.setattr(engine, "sites_in_file", lambda *args: [site(sources[0], 3)])
    monkeypatch.setattr(engine, "form_digests", lambda *args: {})
    monkeypatch.setattr(engine, "load_history", lambda *args: History({}, {}))
    monkeypatch.setattr(engine, "test_plan", lambda *args: (["suite"], tmp_path))
    published = []
    monkeypatch.setattr(engine, "write_results", lambda *args: published.append(args))
    runner = Runner()
    runner.corrupt = failure == "corrupt"
    runner.remove = None if failure == "corrupt" else failure
    with WorkerPool(tmp_path, sources) as pool:
        result = engine.mutate_file(
            sources[0], tmp_path, runner=runner, covered_lines={1}, ignore_coverage=False,
            mutate_all=True, lines=None, test_command=None, timeout_factor=10,
            mutation_warning=50, baselines={}, max_workers=1, worker_pool=pool)
    assert result.baseline_failed
    assert result.written == []
    assert not published
    assert snapshot.read_bytes() == b"previous snapshot\n"
    assert all(source.read_bytes() == b"return 1\n" for source in sources)


def test_pool_grows_and_reuses_existing_checks(tmp_path, monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 3)
    sources = fixture(tmp_path)
    runner = Runner()
    with WorkerPool(tmp_path, sources) as pool:
        run_mutants(tmp_path, sources[0], sources[0].read_bytes(), [site(sources[0], 2)],
                    1, runner, "suite", tmp_path, 2, "a.lua", {}, pool=pool)
        assert len(pool.directories) == 1
        run(tmp_path, sources[1], runner, pool)
        assert len(pool.directories) == 2
        assert len(runner.checks) == 2


def test_cwd_identity_checks_again_and_rejects_linked_cwd(tmp_path):
    sources = fixture(tmp_path)
    runner = Runner()
    (tmp_path / "other").mkdir()
    class CwdRunner:
        def __init__(self):
            self.calls = []
        def run(self, command, cwd, timeout):
            self.calls.append(cwd)
            return CommandResult(0, False, .01, "")
    with WorkerPool(tmp_path, sources) as pool:
        run(tmp_path, sources[0], runner, pool)
        directory = pool.directories[0]
        nested = CwdRunner()
        pool.check(directory, tmp_path, tmp_path / "src", nested, "suite", 2)
        pool.check(directory, tmp_path, tmp_path / "src", nested, "suite", 2)
        assert nested.calls == [directory / "src"]
        if (directory / "other").is_symlink():
            with pytest.raises(WorkerBaselineError, match="escapes"):
                pool.check(directory, tmp_path, tmp_path / "other", nested, "suite", 2)


def test_linked_test_input_change_rejected(tmp_path):
    sources = fixture(tmp_path)
    tests = tmp_path / "test"
    tests.mkdir()
    spec = tests / "value_spec.lua"
    spec.write_text("assert(true)")
    with WorkerPool(tmp_path, sources) as pool:
        run(tmp_path, sources[0], Runner(), pool)
        spec.write_text("error('changed')")
        with pytest.raises(WorkerBaselineError, match="Project inputs"):
            run(tmp_path, sources[1], Runner(), pool)


def test_failed_worker_never_checks_next_command_on_dirty_data(tmp_path):
    sources = fixture(tmp_path)
    runner = Runner()
    runner.corrupt = True
    with WorkerPool(tmp_path, sources) as pool:
        chosen = [site(sources[0], value) for value in (2, 3)]
        plans = {chosen[0].mutation_id: ("first", 2), chosen[1].mutation_id: ("second", 2)}
        with pytest.raises(WorkerBaselineError):
            run_mutants(tmp_path, sources[0], sources[0].read_bytes(), chosen, 1,
                        runner, "suite", tmp_path, 2, "a.lua", {}, plans=plans, pool=pool)
        assert [command for command, _ in runner.checks] == ["first"]


@pytest.mark.parametrize("change", ["bytes", "link", "extra", "original"])
def test_reuse_rejects_binding_or_restoration_changes(tmp_path, change):
    sources = fixture(tmp_path)
    with WorkerPool(tmp_path, sources) as pool:
        run(tmp_path, sources[0], Runner(), pool)
        worker = pool.directories[0]
        destination = worker / "src" / "a.lua"
        if change == "bytes":
            destination.write_bytes(b"return 9\n")
        elif change == "link":
            destination.unlink()
            destination.symlink_to(sources[0])
        elif change == "extra":
            (worker / "cache").write_text("stale runtime state")
        else:
            sources[0].write_bytes(b"return 9\n")
        outcomes = {}
        with pytest.raises(WorkerBaselineError):
            run(tmp_path, sources[1], Runner(), pool, outcomes=outcomes)
        assert outcomes == {}
