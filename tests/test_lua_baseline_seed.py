"""A fresh coverage run seeds the baseline; a reused index does not."""
from threading import Lock

from mutator.engine import mutate_file
from mutator.runner import CommandResult

SOURCE = """local M = {}
function M.add(x)
  return x + 1
end
return M
"""


class Index:
    def __init__(self, directory, fresh, seconds):
        self.directory = directory
        self.command = ["lua54", "-e", "run_busted()", "--", "busted", "--ignore-lua"]
        self.fresh = fresh
        self.seconds = seconds

    def select(self, source, line):
        return None


class RecordingRunner:
    verbose = False

    def __init__(self, root):
        self.root = root
        self.original = SOURCE
        self.baselines = []
        self.mutants = []
        self.lock = Lock()

    def run(self, command, cwd, timeout):
        text = (cwd / "src" / "calc.lua").read_text(encoding="utf-8")
        with self.lock:
            (self.baselines if cwd == self.root else self.mutants).append((command, timeout))
        return CommandResult(int(text != self.original), False, 1.5, "")


def project(root):
    source = root / "src" / "calc.lua"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    return source


def mutate(source, root, runner, index):
    return mutate_file(
        source, root,
        runner=runner, covered_lines={3}, ignore_coverage=False,
        mutate_all=True, lines=None, test_command=None, timeout_factor=10,
        mutation_warning=100, baselines={}, max_workers=1,
        lua_coverage=index, select_specs=False,
    )


def test_fresh_coverage_seeds_the_baseline(tmp_path):
    source = project(tmp_path)
    runner = RecordingRunner(tmp_path)
    result = mutate(source, tmp_path, runner, Index(tmp_path, fresh=True, seconds=3.0))
    assert not result.baseline_failed
    assert runner.baselines == []
    assert runner.mutants
    assert all(timeout == 30.0 for _command, timeout in runner.mutants)


def test_reused_index_still_runs_the_baseline(tmp_path):
    source = project(tmp_path)
    runner = RecordingRunner(tmp_path)
    result = mutate(source, tmp_path, runner, Index(tmp_path, fresh=False, seconds=3.0))
    assert not result.baseline_failed
    assert len(runner.baselines) == 1
    assert all(timeout == 15.0 for _command, timeout in runner.mutants)
