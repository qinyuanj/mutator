"""One pristine command run per run; other workers validated structurally."""
from mutator.model import Site
from mutator.runner import CommandResult
from mutator.workers import WorkerPool, run_mutants


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
        self.pristine = []

    def run(self, command, cwd, timeout):
        values = [(cwd / "src" / name).read_bytes() for name in ("a.lua", "b.lua")]
        if values == [b"return 1\n", b"return 1\n"]:
            self.pristine.append(cwd)
            return CommandResult(0, False, .01, "")
        return CommandResult(17 if b"return 3\n" in values else 0, False, .01, "")


def run(root, source, runner, pool, outcomes=None):
    outcomes = {} if outcomes is None else outcomes
    run_mutants(root, source, source.read_bytes(), [site(source, 2), site(source, 3)],
                2, runner, "suite", root, 2, source.name, outcomes, pool=pool)
    return outcomes


def expected_outcomes():
    return [
        {"a.lua2": "survived", "a.lua3": "killed"},
        {"b.lua2": "survived", "b.lua3": "killed"},
    ]


def test_shared_check_runs_one_pristine_command_per_run(tmp_path):
    sources = fixture(tmp_path)
    runner = Runner()
    with WorkerPool(tmp_path, sources, check_each_worker=False) as pool:
        actual = [run(tmp_path, source, runner, pool) for source in sources]
    assert actual == expected_outcomes()
    # Two files, two workers each, one command: one pristine run for the pool.
    assert len(runner.pristine) == 1
    assert all(source.read_bytes() == b"return 1\n" for source in sources)


def test_paranoid_pool_checks_every_worker(tmp_path):
    sources = fixture(tmp_path)
    runner = Runner()
    with WorkerPool(tmp_path, sources, check_each_worker=True) as pool:
        actual = [run(tmp_path, source, runner, pool) for source in sources]
    assert actual == expected_outcomes()
    # Both workers are checked once; the pool caches the checks across files.
    assert len(runner.pristine) == 2
