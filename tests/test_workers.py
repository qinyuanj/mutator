"""Worker acceptance tests: private overlays, cleanup, and concurrent mutants."""

import subprocess
import sys
from pathlib import Path

import pytest

from mutator.workers import create_workers, delete_tree, run_mutants
from mutator.model import Site
from mutator.runner import CommandRunner


def project(root):
    (root / "src").mkdir()
    (root / "src" / "value.py").write_bytes(b"value = 1\n")
    (root / "assets").mkdir()
    (root / "assets" / "data.txt").write_text("original")
    (root / "pyproject.toml").write_text("[tool.demo]\n")
    return root / "src" / "value.py"


def test_worker_links_untouched_files_and_copies_the_source(tmp_path):
    source = project(tmp_path)
    base = tmp_path / "target" / "mutation-workers" / "run-test"
    worker, = create_workers(base, tmp_path, "src/value.py", source.read_bytes(), 1)
    try:
        assert (worker / "assets").is_symlink()
        assert (worker / "src" / "value.py").is_symlink() is False
        (worker / "src" / "value.py").write_bytes(b"value = 2\n")
        assert source.read_bytes() == b"value = 1\n"
        assert (tmp_path / "assets" / "data.txt").read_text() == "original"
    finally:
        delete_tree(base)
    assert source.read_bytes() == b"value = 1\n"


def test_link_failures_are_visible_and_cleaned(monkeypatch, tmp_path):
    source = project(tmp_path)
    error = OSError("link failure")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(Path, "symlink_to", fail)
    base = tmp_path / "workers"
    with pytest.raises(OSError) as raised:
        create_workers(base, tmp_path, "src/value.py", source.read_bytes(), 2)
    assert raised.value is error
    assert not base.exists()
    assert source.read_bytes() == b"value = 1\n"


def test_concurrent_mutants_use_private_workers(monkeypatch, tmp_path):
    source = project(tmp_path)
    original = source.read_bytes()
    rendezvous = tmp_path / "rendezvous"
    rendezvous.mkdir()
    # The script runs from -c so sys.path[0] is the worker, not a symlink's
    # target. The subprocess barrier requires two workers alive at once.
    script = """
import sys, time
from pathlib import Path
from src.value import value
if value == 1:
    sys.exit(0)  # Pristine worker baseline does not enter the mutation barrier.
assert value in (2, 3)
original = Path(sys.argv[1])
assert original.read_bytes() == b"value = 1\\n"
barrier = Path(sys.argv[2])
(barrier / str(value)).write_text("ready")
deadline = time.monotonic() + 8
while len(list(barrier.iterdir())) < 2:
    assert time.monotonic() < deadline, "workers did not run concurrently"
    assert original.read_bytes() == b"value = 1\\n"
    time.sleep(.01)
assert Path("src/value.py").read_text() == f"value = {value}\\n"
assert original.read_bytes() == b"value = 1\\n"
sys.exit(0 if value == 2 else 17)
"""
    monkeypatch.setattr("os.cpu_count", lambda: 2)
    sites = [Site("src/value.py", "value", "defn/value", "value", 1,
                  8, 9, "1", str(value), "number", str(value)) for value in (2, 3)]
    outcomes = {}
    run_mutants(tmp_path, source, original, sites, 2, CommandRunner(),
                [sys.executable, "-c", script, str(source), str(rendezvous)],
                tmp_path, 12, "src/value.py", outcomes)
    assert outcomes == {"2": "survived", "3": "killed"}
    assert {p.name for p in rendezvous.iterdir()} == {"2", "3"}
    assert source.read_bytes() == original
    assert not list((tmp_path / "target" / "mutation-workers").iterdir())


def test_runtime_failure_removes_workers(tmp_path):
    source = project(tmp_path)
    command = [sys.executable, "-c",
               "from src.value import value; import time; time.sleep(10) if value != 1 else None"]
    site = Site("src/value.py", "value", "defn/value", "value", 1,
                8, 9, "1", "2", "number", "two")
    outcomes = {}
    run_mutants(tmp_path, source, source.read_bytes(), [site], 1,
                CommandRunner(), command, tmp_path, .2, "src/value.py", outcomes,
                check_timeout=5.0)
    assert outcomes == {"two": "killed"}
    assert source.read_bytes() == b"value = 1\n"
    assert (tmp_path / "assets" / "data.txt").read_text() == "original"
    assert not list((tmp_path / "target" / "mutation-workers").iterdir())


def test_copied_node_relative_importers_execute_mutant(tmp_path):
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    (tmp_path / "src").mkdir()
    (tmp_path / "test").mkdir()
    source = tmp_path / "src" / "value.mjs"
    original = b"export const value = 1;\n"
    source.write_bytes(original)
    (tmp_path / "test" / "bridge.mjs").write_text(
        "export { value } from '../src/value.mjs';\n")
    (tmp_path / "test" / "check.mjs").write_text(
        "import { value } from './bridge.mjs';\nif (![1, 2].includes(value)) process.exit(19);\n")
    (tmp_path / "package.json").write_text('{"type":"module"}')
    site = Site("src/value.mjs", "value", "defn/value", "value", 1,
                21, 22, "1", "2", "number", "two")
    outcomes = {}
    run_mutants(tmp_path, source, original, [site], 1, CommandRunner(),
                [node, "test/check.mjs"], tmp_path, 5, "src/value.mjs", outcomes)
    assert outcomes == {"two": "survived"}
    assert source.read_bytes() == original


def test_runner_exception_cleans_worker(tmp_path):
    source = project(tmp_path)

    class FailingRunner:
        verbose = False

        def run(self, command, cwd, timeout):
            if (cwd / "src" / "value.py").read_text() == "value = 1\n":
                from mutator.runner import CommandResult
                return CommandResult(0, False, .01, "")
            assert (cwd / "src" / "value.py").read_text() == "value = 2\n"
            raise RuntimeError("runner failed")

    site = Site("src/value.py", "value", "defn/value", "value", 1,
                8, 9, "1", "2", "number", "two")
    with pytest.raises(RuntimeError, match="runner failed"):
        run_mutants(tmp_path, source, source.read_bytes(), [site], 1,
                    FailingRunner(), "unused", tmp_path, 1, "src/value.py", {})
    assert not list((tmp_path / "target" / "mutation-workers").iterdir())


@pytest.mark.parametrize("status", [0, 3, 17])
def test_custom_command_preserves_arguments_and_process_status(tmp_path, status):
    (tmp_path / "check.py").write_text(
        "import sys\nassert sys.argv[1:] == ['two words', 'a&b']\n"
        f"sys.exit({status})\n")
    result = CommandRunner().run(
        f'"{sys.executable}" check.py "two words" "a&b"', tmp_path, 5)
    assert result.code == status
    assert not result.timed_out
