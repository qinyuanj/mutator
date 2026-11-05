"""A mutant's file writes stay in its own view (issue #25)."""

import sys

from mutator.model import Site
from mutator.runner import CommandRunner
from mutator.workers import WorkerSlots, run_mutants


def project(root):
    (root / "pkg").mkdir()
    (root / "other").mkdir()
    (root / "other" / "keep.txt").write_text("keep")
    source = root / "pkg" / "value.py"
    source.write_bytes(b"value = 1\n")
    return source


# Writes next to the cwd and through a sibling directory, the two leaks of
# the issue, then fails: the mutant is killed, its files must not survive.
LEAKY = """
import sys
from pathlib import Path
sys.path.insert(0, ".")
from value import value
if value != 1:
    Path("leak").write_text("x")
    Path("../other/leak").write_text("x")
    Path("../other/keep.txt").write_text("changed")
    sys.exit(1)
assert not Path("leak").exists() and not Path("../other/leak").exists()
"""


def sites():
    return [Site("pkg/value.py", "value", "defn/value", "value", 1,
                 8, 9, "1", str(value), "number", str(value)) for value in (2, 3)]


def test_a_killed_mutants_writes_reach_neither_the_project_nor_the_next_mutant(tmp_path):
    source = project(tmp_path)
    outcomes = {}
    run_mutants(tmp_path, source, source.read_bytes(), sites(), 1, CommandRunner(),
                [sys.executable, "-c", LEAKY], tmp_path / "pkg", 10, "pkg/value.py", outcomes)
    assert outcomes == {"2": "killed", "3": "killed"}
    assert not (tmp_path / "pkg" / "leak").exists()
    assert not (tmp_path / "other" / "leak").exists()
    assert (tmp_path / "other" / "keep.txt").read_text() == "keep"
    assert source.read_bytes() == b"value = 1\n"


def test_a_rust_slot_keeps_running_after_a_mutant_leaks_a_file(tmp_path):
    source = project(tmp_path)
    outcomes = {}
    with WorkerSlots(tmp_path) as slots:
        run_mutants(tmp_path, source, source.read_bytes(), sites(), 1, CommandRunner(),
                    [sys.executable, "-c", LEAKY], tmp_path / "pkg", 10, "pkg/value.py",
                    outcomes, pool=slots)
    assert outcomes == {"2": "killed", "3": "killed"}
    assert not (tmp_path / "pkg" / "leak").exists()
    assert not (tmp_path / "other" / "leak").exists()
