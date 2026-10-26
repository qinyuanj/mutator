"""Mutants of several Lua files share one worker queue."""
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from mutator.model import History, Site
from mutator.runner import CommandResult
from mutator.workers import QueuedFile, WorkerBaselineError, WorkerPool, run_queue

NAMES = ("a.lua", "b.lua")


def fixture(root):
    (root / "src").mkdir()
    sources = [root / "src" / name for name in NAMES]
    for source in sources:
        source.write_bytes(b"return 1\n")
    return sources


def site(source, value):
    return Site(source.name, "value", "defn/value", "value", 1,
                7, 8, "1", str(value), "number", source.name + str(value))


def queued(source, values, command="suite", check="check"):
    return QueuedFile(
        relative="src/" + source.name, original=source.read_bytes(),
        sites=[site(source, value) for value in values], command=command,
        cwd=source.parent.parent, timeout=2, file_key=source.name,
        check_command=check, check_timeout=2,
    )


def mutated(cwd):
    """The (file, value) a worker currently holds, or None when pristine."""

    for name in NAMES:
        text = (cwd / "src" / name).read_bytes()
        if text != b"return 1\n":
            return name, text
    return None


class Runner:
    """Value 3 kills; value 9 waits until b.lua is published."""

    verbose = False

    def __init__(self):
        self.lock = Lock()
        self.events = []
        self.b_started = Event()
        self.b_published = Event()
        self.corrupt = False

    def run(self, command, cwd, timeout):
        held = mutated(cwd)
        with self.lock:
            self.events.append(("start", command, held))
        if held is None:
            return CommandResult(0, False, .01, "")
        name, text = held
        if name == "b.lua":
            self.b_started.set()
        if text == b"return 9\n":
            overlapped = self.b_published.wait(timeout=5)
            with self.lock:
                self.events.append(("slow-done", overlapped))
        if self.corrupt and name == "b.lua":
            (cwd / "src" / "a.lua").write_bytes(b"broken\n")
        return CommandResult(17 if text == b"return 3\n" else 0, False, .01, "")


def test_next_file_starts_while_previous_file_runs(tmp_path):
    sources = fixture(tmp_path)
    runner = Runner()
    done = []

    def publish(item):
        done.append((item.file_key, dict(item.completed)))
        if item.file_key == "b.lua":
            runner.b_published.set()

    files = [queued(sources[0], [9, 3]), queued(sources[1], [2, 3])]
    with WorkerPool(tmp_path, sources, check_each_worker=False) as pool:
        run_queue(pool, files, 2, runner, publish)
    # a.lua's slow mutant was still running while b.lua ran and was published.
    assert ("slow-done", True) in runner.events
    assert done == [("b.lua", {"b.lua2": "survived", "b.lua3": "killed"}),
                    ("a.lua", {"a.lua9": "survived", "a.lua3": "killed"})]
    assert all(source.read_bytes() == b"return 1\n" for source in sources)
    assert not list((tmp_path / "target" / "mutation-workers").iterdir())


def test_queue_preserves_each_files_survivor_first_order(tmp_path):
    sources = fixture(tmp_path)
    runner = Runner()
    files = [queued(sources[0], [2, 3]), queued(sources[1], [4, 3])]
    with WorkerPool(tmp_path, sources, check_each_worker=False) as pool:
        run_queue(pool, files, 1, runner, lambda item: None)
    order = [event[2] for event in runner.events if event[0] == "start" and event[2] is not None]
    assert [(name, text[7:8]) for name, text in order] == [
        ("a.lua", b"2"), ("a.lua", b"3"), ("b.lua", b"4"), ("b.lua", b"3")]


@pytest.mark.parametrize("paranoid,expected", [(False, 2), (True, 4)])
def test_pristine_checks_precede_mutants_once_per_command(tmp_path, paranoid, expected):
    sources = fixture(tmp_path)
    runner = Runner()
    files = [queued(sources[0], [2, 3], check="one"), queued(sources[1], [2, 3], check="two")]
    with WorkerPool(tmp_path, sources, check_each_worker=paranoid) as pool:
        run_queue(pool, files, 2, runner, lambda item: None)
    pristine = [event for event in runner.events if event[0] == "start" and event[2] is None]
    assert len(pristine) == expected
    assert {event[1] for event in pristine} == {"one", "two"}
    first_mutant = next(i for i, event in enumerate(runner.events) if event[0] == "start" and event[2])
    assert all(event[2] is None for event in runner.events[:first_mutant])


def test_worker_failure_publishes_only_completed_files(tmp_path):
    sources = fixture(tmp_path)
    runner = Runner()
    runner.corrupt = True
    done = []
    files = [queued(sources[0], [2]), queued(sources[1], [2, 3])]
    with WorkerPool(tmp_path, sources, check_each_worker=False) as pool:
        with pytest.raises(WorkerBaselineError):
            run_queue(pool, files, 1, runner, lambda item: done.append(item.file_key))
        assert pool.failed
    assert done == ["a.lua"]
    assert all(source.read_bytes() == b"return 1\n" for source in sources)


def test_failed_publication_stops_the_queue(tmp_path):
    sources = fixture(tmp_path)
    runner = Runner()
    files = [queued(sources[0], [2]), queued(sources[1], [2, 3])]

    def publish(item):
        raise WorkerBaselineError("publication failed")

    with WorkerPool(tmp_path, sources, check_each_worker=False) as pool:
        with pytest.raises(WorkerBaselineError, match="publication"):
            run_queue(pool, files, 1, runner, publish)
    assert not any(event[2] and event[2][0] == "b.lua" for event in runner.events if event[0] == "start")


def engine_fixture(tmp_path, monkeypatch, failing=None):
    from mutator import engine

    sources = fixture(tmp_path)
    monkeypatch.setattr(engine, "ensure_crapper", lambda: SimpleNamespace(
        discover=SimpleNamespace(language_of=lambda path: "lua")))
    monkeypatch.setattr(engine, "sites_in_file", lambda source, path, root, key: [
        site(path, 2), site(path, 3)])
    monkeypatch.setattr(engine, "form_digests", lambda *args: {})
    monkeypatch.setattr(engine, "load_history", lambda *args: History({}, {}))
    monkeypatch.setattr(engine, "_forms", lambda *args: [])
    # A per-file command, so one file's baseline can fail on its own.
    monkeypatch.setattr(engine, "test_plan", lambda root, path, *args: ([path.name], tmp_path))
    published = []
    monkeypatch.setattr(engine, "write_results",
                        lambda root, key, forms, outcomes: published.append((key, dict(outcomes))) or [])
    return engine, sources, published


class EngineRunner(Runner):
    def __init__(self, failing=None):
        super().__init__()
        self.failing = failing

    def run(self, command, cwd, timeout):
        if mutated(cwd) is None and command == [self.failing]:
            return CommandResult(1, False, .01, "baseline broke")
        return super().run(command, cwd, timeout)


def mutate(engine, tmp_path, sources, runner, pool, max_workers=2):
    return engine.mutate_queued(
        [(source, {1}, None) for source in sources], tmp_path, runner=runner,
        ignore_coverage=False, mutate_all=True, lines=None, timeout_factor=10,
        mutation_warning=50, baselines={}, max_workers=max_workers, busted_task=None, worker_pool=pool)


def test_engine_baseline_failure_does_not_block_other_files(tmp_path, monkeypatch):
    engine, sources, published = engine_fixture(tmp_path, monkeypatch)
    with WorkerPool(tmp_path, sources, check_each_worker=False) as pool:
        results = mutate(engine, tmp_path, sources, EngineRunner(failing="a.lua"), pool)
    assert [result.path for result in results] == ["src/a.lua", "src/b.lua"]
    assert results[0].baseline_failed and "baseline broke" in results[0].baseline_message
    assert not results[1].baseline_failed
    assert published == [("src/b.lua", {"b.lua2": "survived", "b.lua3": "killed"})]


def test_engine_worker_failure_keeps_completed_snapshots(tmp_path, monkeypatch):
    engine, sources, published = engine_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "sites_in_file", lambda source, path, root, key: [
        site(path, 2)] if path.name == "a.lua" else [site(path, 2), site(path, 3)])
    runner = EngineRunner()
    runner.corrupt = True
    with WorkerPool(tmp_path, sources, check_each_worker=False) as pool:
        results = mutate(engine, tmp_path, sources, runner, pool, max_workers=1)
    assert not results[0].baseline_failed
    assert results[1].baseline_failed and "restoration" in results[1].baseline_message
    assert published == [("src/a.lua", {"a.lua2": "survived"})]
    assert all(source.read_bytes() == b"return 1\n" for source in sources)
    assert not (tmp_path / "target" / "mutator-backup").exists()


class EditingEngineRunner(EngineRunner):
    """Saves or deletes a.lua in the project tree while one of its mutants runs."""

    def __init__(self, root, delete=False):
        super().__init__()
        self.root = root
        self.delete = delete

    def run(self, command, cwd, timeout):
        held = mutated(cwd)
        source = self.root / "src" / "a.lua"
        if held and held[0] == "a.lua" and source.exists():
            if self.delete:
                source.unlink()
            else:
                source.write_bytes(b"return 2\n")
        return super().run(command, cwd, timeout)


@pytest.mark.parametrize("delete, change", [(False, "changed"), (True, "was deleted")],
                         ids=["edited", "deleted"])
def test_engine_edit_during_the_queue_keeps_it_and_the_other_snapshots(tmp_path, monkeypatch, delete, change):
    engine, sources, published = engine_fixture(tmp_path, monkeypatch)
    with WorkerPool(tmp_path, sources, check_each_worker=False) as pool:
        results = mutate(engine, tmp_path, sources, EditingEngineRunner(tmp_path, delete), pool,
                         max_workers=1)
    assert f"src/a.lua {change} during the mutation run" in results[0].source_changed
    assert results[0].written == []
    assert not results[1].source_changed
    assert published == [("src/b.lua", {"b.lua2": "survived", "b.lua3": "killed"})]
    assert (not sources[0].exists()) if delete else sources[0].read_bytes() == b"return 2\n"
    assert sources[1].read_bytes() == b"return 1\n"


def test_queue_outcomes_match_file_by_file_runs(tmp_path):
    from mutator.workers import run_mutants

    sources = fixture(tmp_path)
    expected = {}
    for source in sources:
        run_mutants(tmp_path, source, source.read_bytes(), [site(source, v) for v in (2, 3, 4)],
                    2, Runner(), "suite", tmp_path, 2, source.name, expected)
    files = [queued(source, [2, 3, 4]) for source in sources]
    with WorkerPool(tmp_path, sources, check_each_worker=False) as pool:
        run_queue(pool, files, 3, Runner(), lambda item: None)
    assert {k: v for item in files for k, v in item.completed.items()} == expected


@pytest.mark.parametrize("args,path,shared", [
    ([], "src/a.lua", True),
    (["--select-specs"], "src/a.lua", False),
    (["--test-command", "make test"], "src/a.lua", False),
    ([], "src/a.py", False),
])
def test_only_full_suite_lua_files_share_the_queue(args, path, shared):
    from pathlib import Path

    from mutator.cli import _shares_queue, parse_args

    assert _shares_queue(parse_args(args), Path(path)) is shared


def test_interrupt_keeps_completed_files_and_restores_sources(tmp_path):
    class InterruptRunner(Runner):
        def run(self, command, cwd, timeout):
            held = mutated(cwd)
            if held is not None and held[0] == "b.lua":
                raise KeyboardInterrupt
            return super().run(command, cwd, timeout)

    sources = fixture(tmp_path)
    done = []
    files = [queued(sources[0], [2]), queued(sources[1], [2, 3])]
    with WorkerPool(tmp_path, sources, check_each_worker=False) as pool:
        with pytest.raises(KeyboardInterrupt):
            run_queue(pool, files, 1, InterruptRunner(), lambda item: done.append(item.file_key))
    assert done == ["a.lua"]
    assert all(source.read_bytes() == b"return 1\n" for source in sources)
    assert not list((tmp_path / "target" / "mutation-workers").iterdir())
