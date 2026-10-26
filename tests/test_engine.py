import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mutator.engine import StaleBackupsError, mutate_file, restore_backups
from mutator.edn import loads
from mutator.metrics import snapshot_path
from mutator.runner import CommandResult
from mutator.workers import create_workers, delete_tree, new_run_dir, worker_count


class FakeRunner:
    def __init__(self):
        self.verbose = False
        self.calls = 0
        self._lock = threading.Lock()

    def run(self, command, cwd, timeout):
        with self._lock:
            self.calls += 1
        text = (cwd / "src" / "demo.py").read_text(encoding="utf-8")
        namespace: dict = {}
        exec(text, namespace)
        ok = namespace["add"](1, 2) == 3
        return CommandResult(code=0 if ok else 1, timed_out=False, seconds=0.01, output="")


SOURCE = """def add(a, b):
    if a > 0:
        return a + b
    return a + b
"""


def test_mutants_are_killed_or_kept_and_the_snapshot_is_differential(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")
    original = path.read_bytes()
    runner = FakeRunner()
    first = mutate_file(
        path,
        tmp_path,
        runner=runner,
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
    )
    assert path.read_bytes() == original
    assert first.baseline_failed is False
    form = first.forms[0]
    assert form.id == "defn/add"
    assert form.namespace == "demo"
    # `>` and the unexecuted `+` survive. The executed `+` is killed. `0` -> `1`
    # still returns 3 for add(1, 2).
    assert form.killed == 1
    assert form.survived == 3
    assert form.sites == 4
    assert form.killed + form.survived + form.uncovered == form.sites
    data = loads(snapshot_path(tmp_path, "demo").read_text(encoding="utf-8"))
    assert data["namespace"] == "demo"
    assert data["forms"][0]["id"] == "defn/add"
    assert data["forms"][0]["killed"] == 1
    assert data["forms"][0]["survived"] == 3
    first_calls = runner.calls

    second = mutate_file(
        path,
        tmp_path,
        runner=runner,
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=False,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
    )
    assert second.forms[0].killed == 1
    assert second.forms[0].survived == 3
    # Main baseline, pristine overlays, and three survivors; the kill is cached.
    assert runner.calls - first_calls == 4 + worker_count(3, None)


def test_uncovered_sites_are_not_executed(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")
    runner = FakeRunner()
    result = mutate_file(
        path,
        tmp_path,
        runner=runner,
        covered_lines=set(),
        ignore_coverage=False,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
    )
    assert runner.calls == 0
    assert result.forms[0].uncovered == 4
    assert result.forms[0].killed == 0
    assert result.forms[0].sites == 4


def test_a_failed_baseline_does_not_rewrite_metrics(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")

    class Red:
        verbose = False

        def run(self, command, cwd, timeout):
            return CommandResult(code=1, timed_out=False, seconds=0.01, output="nope")

    result = mutate_file(
        path,
        tmp_path,
        runner=Red(),
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
    )
    assert result.baseline_failed
    assert not snapshot_path(tmp_path, "demo").exists()


def test_a_baseline_that_ran_no_tests_fails(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")

    class Empty:
        verbose = False

        def run(self, command, cwd, timeout):
            output = "\n----------------------------------------------------------------------\nRan 0 tests in 0.000s\n\nOK\n"
            return CommandResult(code=0, timed_out=False, seconds=0.01, output=output)

    result = mutate_file(
        path,
        tmp_path,
        runner=Empty(),
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
    )
    assert result.baseline_failed
    assert "Ran 0 tests" in result.baseline_message
    assert not snapshot_path(tmp_path, "demo").exists()


@pytest.mark.parametrize("timed_out", [False, True])
def test_failed_pristine_worker_baseline_preserves_metrics(tmp_path, timed_out):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")
    options = dict(covered_lines=None, ignore_coverage=True, mutate_all=True, lines=None,
                   test_command="fake", timeout_factor=10, mutation_warning=50,
                   baselines={}, max_workers=1)
    mutate_file(path, tmp_path, runner=FakeRunner(), **options)
    snapshot = snapshot_path(tmp_path, "demo")
    previous = snapshot.read_bytes()

    class BrokenWorker:
        verbose = False

        def run(self, command, cwd, timeout):
            if cwd == tmp_path:
                return CommandResult(0, False, 0.01, "")
            assert (cwd / "src" / "demo.py").read_text(encoding="utf-8") == SOURCE, "mutated before worker baseline"
            return CommandResult(124 if timed_out else 1, timed_out, 0.01,
                                 "helper setup failed before test execution")

    result = mutate_file(path, tmp_path, runner=BrokenWorker(), **{**options, "baselines": {}})
    assert result.baseline_failed
    assert "worker-0" in result.baseline_message
    assert "helper setup failed before test execution" in result.baseline_message
    logs = list((tmp_path / "target" / "mutation-logs").glob("run-*-worker-0.log"))
    assert len(logs) == 1 and str(logs[0]) in result.baseline_message
    assert logs[0].read_text(encoding="utf-8") == "helper setup failed before test execution"
    assert result.forms == [] and result.written == []
    assert snapshot.read_bytes() == previous
    assert path.read_text(encoding="utf-8") == SOURCE
    assert not list((tmp_path / "target" / "mutation-workers").iterdir())


def test_outstanding_workers_and_execution_failure_preserve_previous_results(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")
    options = dict(
        covered_lines=None, ignore_coverage=True, mutate_all=True, lines=None,
        test_command="fake", timeout_factor=10, mutation_warning=50, baselines={}, max_workers=2,
    )
    mutate_file(path, tmp_path, runner=FakeRunner(), **options)
    snapshot = snapshot_path(tmp_path, "demo")
    original = snapshot.read_bytes()
    entered = threading.Event()
    release = threading.Event()

    class Blocked:
        verbose = False

        def run(self, command, cwd, timeout):
            if timeout is not None:
                entered.set()
                assert release.wait(10), "worker was not released"
                raise OSError("worker execution failed")
            return CommandResult(code=0, timed_out=False, seconds=0.01, output="")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(mutate_file, path, tmp_path, runner=Blocked(), **options)
        try:
            assert entered.wait(10), "worker did not start"
            assert not future.done()
            assert snapshot.read_bytes() == original
        finally:
            release.set()
        with pytest.raises(OSError, match="worker execution failed"):
            future.result(timeout=10)
    assert snapshot.read_bytes() == original
    assert path.read_text(encoding="utf-8") == SOURCE


def test_worker_count_follows_sites_cores_and_the_requested_cap():
    cores = os.cpu_count() or 1
    assert worker_count(10, None) == min(10, cores)
    assert worker_count(10, 3) == min(3, cores)
    assert worker_count(2, 8) == min(2, cores)


def test_workers_keep_a_private_copy_and_link_the_rest(tmp_path):
    source = tmp_path / "src" / "demo.py"
    source.parent.mkdir()
    source.write_text("def place(x):\n    return x > 0\n", encoding="utf-8")
    sibling = tmp_path / "src" / "other.py"
    sibling.write_text("kept = True\n", encoding="utf-8")
    marker = tmp_path / "tests" / "keep.txt"
    marker.parent.mkdir()
    marker.write_text("keep", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    original = source.read_bytes()

    base = new_run_dir(tmp_path)
    try:
        workers = create_workers(base, tmp_path, "src/demo.py", original, 2)
        assert len(workers) == 2
        for worker in workers:
            private = worker / "src" / "demo.py"
            assert private.is_file()
            assert not private.is_symlink()
            assert private.read_bytes() == original
            assert (worker / "src" / "other.py").is_symlink()
            assert (worker / "tests").is_symlink()
            assert (worker / "tests" / "keep.txt").read_text(encoding="utf-8") == "keep"
            assert (worker / "pyproject.toml").is_file()
            assert not (worker / "pyproject.toml").is_symlink()
        (workers[0] / "src" / "demo.py").write_text("changed\n", encoding="utf-8")
        assert workers[1].joinpath("src/demo.py").read_bytes() == original
        assert source.read_bytes() == original
    finally:
        delete_tree(base)

    assert source.read_bytes() == original
    assert sibling.read_text(encoding="utf-8") == "kept = True\n"
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not base.exists()


def test_node_imports_the_worker_copy_through_a_relative_specifier(tmp_path):
    source = tmp_path / "src" / "find.mjs"
    source.parent.mkdir()
    source.write_text('export const value = "real"\n', encoding="utf-8")
    beside = tmp_path / "src" / "find.test.mjs"
    beside.write_text('import { value } from "./find.mjs";\nconsole.log(value)\n', encoding="utf-8")
    (tmp_path / "src" / "notes.mjs").write_text("export const notes = 1\n", encoding="utf-8")
    main = tmp_path / "src" / "main.mjs"
    main.write_text('import { value } from "./find.mjs";\nexport const main = value\n', encoding="utf-8")
    (tmp_path / "src" / "app.test.mjs").write_text('import { main } from "./main.mjs";\n', encoding="utf-8")
    distant = tmp_path / "tests" / "use.mjs"
    distant.parent.mkdir()
    distant.write_text('import { value } from "../src/find.mjs";\nconsole.log(value)\n', encoding="utf-8")
    (tmp_path / "tests" / "other.test.mjs").write_text("export const n = 1\n", encoding="utf-8")

    base = new_run_dir(tmp_path)
    try:
        worker = create_workers(base, tmp_path, "src/find.mjs", source.read_bytes(), 1)[0]
        private = worker / "src" / "find.mjs"
        private.write_text('export const value = "worker"\n', encoding="utf-8")
        for relative in ("src/find.test.mjs", "src/main.mjs", "src/app.test.mjs", "tests/use.mjs"):
            copied = worker / relative
            assert copied.is_file()
            assert not copied.is_symlink()
        assert (worker / "src" / "notes.mjs").is_symlink()
        assert not (worker / "tests").is_symlink()
        assert (worker / "tests" / "other.test.mjs").is_symlink()
        if shutil.which("node"):
            for script in ("src/find.test.mjs", "tests/use.mjs"):
                completed = subprocess.run(
                    ["node", script],
                    cwd=worker,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                assert completed.returncode == 0
                assert completed.stdout.strip() == "worker"
    finally:
        delete_tree(base)

    assert beside.read_text(encoding="utf-8").startswith("import")
    assert distant.read_text(encoding="utf-8").startswith("import")
    assert source.read_text(encoding="utf-8") == 'export const value = "real"\n'


def test_a_mutation_run_leaves_the_project_tree_unchanged(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text("def place(x):\n    return x > 0\n", encoding="utf-8")
    original = path.read_text(encoding="utf-8")

    class Record:
        verbose = False

        def run(self, command, cwd, timeout):
            if timeout is not None:
                assert "mutation-workers" in cwd.as_posix()
            mutated = (cwd / "src" / "demo.py").read_text(encoding="utf-8") != original
            return CommandResult(code=int(mutated), timed_out=False, seconds=0.01, output="")

    mutate_file(
        path,
        tmp_path,
        runner=Record(),
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
        max_workers=2,
    )
    assert path.read_text(encoding="utf-8") == original
    assert [item for item in (tmp_path / "target").rglob("*") if item.is_file()] == []


def test_restore_backups_refuses_and_leaves_sources_and_backups_alone(tmp_path):
    source = tmp_path / "src" / "demo.py"
    source.parent.mkdir()
    source.write_text("edited later\n", encoding="utf-8")
    backup = tmp_path / "target" / "mutator-backup" / "src" / "demo.py"
    backup.parent.mkdir(parents=True)
    backup.write_text("original\n", encoding="utf-8")
    with pytest.raises(StaleBackupsError, match="mutator-backup"):
        restore_backups(tmp_path)
    assert source.read_text(encoding="utf-8") == "edited later\n"
    assert backup.read_text(encoding="utf-8") == "original\n"


class EditingRunner(FakeRunner):
    """Changes the project source once its baseline has run in the real tree."""

    def __init__(self, root, edit, *, fail_baseline=False, raise_baseline=False):
        super().__init__()
        self.root = root
        self.edit = edit
        self.fail_baseline = fail_baseline
        self.raise_baseline = raise_baseline

    def run(self, command, cwd, timeout):
        result = super().run(command, cwd, timeout)
        if cwd == self.root and self.edit is not None:
            self.edit(self.root / "src" / "demo.py")
            self.edit = None
            if self.raise_baseline:
                raise OSError("runner broke")
            if self.fail_baseline:
                return CommandResult(code=1, timed_out=False, seconds=0.01, output="")
        return result


def run_editing(tmp_path, runner):
    return mutate_file(
        tmp_path / "src" / "demo.py",
        tmp_path,
        runner=runner,
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
    )


EDITED = SOURCE + "# saved by the user\n"


@pytest.mark.parametrize("edit, kept", [
    (lambda path: path.write_text(EDITED, encoding="utf-8"), EDITED),
    (lambda path: path.unlink(), None),
], ids=["edited", "deleted"])
def test_a_source_changed_during_the_run_is_kept_and_gets_no_snapshot(tmp_path, edit, kept):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")
    result = run_editing(tmp_path, EditingRunner(tmp_path, edit))
    assert "src/demo.py" in result.source_changed
    assert result.written == []
    assert not snapshot_path(tmp_path, "demo").exists()
    if kept is None:
        assert not path.exists()
    else:
        assert path.read_text(encoding="utf-8") == kept


def test_a_baseline_failure_mentions_a_source_edited_during_it(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")
    runner = EditingRunner(tmp_path, lambda source: source.write_text(EDITED, encoding="utf-8"),
                           fail_baseline=True)
    result = run_editing(tmp_path, runner)
    assert result.baseline_failed
    assert "src/demo.py changed during the mutation run" in result.baseline_message
    assert path.read_text(encoding="utf-8") == EDITED


def test_an_exception_keeps_a_source_edited_during_the_run(tmp_path, capsys):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")
    runner = EditingRunner(tmp_path, lambda source: source.write_text(EDITED, encoding="utf-8"),
                           raise_baseline=True)
    with pytest.raises(OSError, match="runner broke"):
        run_editing(tmp_path, runner)
    assert path.read_text(encoding="utf-8") == EDITED
    assert "src/demo.py changed during the mutation run" in capsys.readouterr().err
