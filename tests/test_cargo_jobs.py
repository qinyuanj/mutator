"""Workers share the cores instead of each running nproc Cargo jobs (issue #21)."""

import os
import sys

from mutator.model import Site
from mutator.runner import CommandRunner
from mutator.workers import run_mutants


def _sites(source, count):
    return [
        Site("calc", "add", "defn/add", "add", 2, 22, 23, "1", str(value), "number", f"m{value}")
        for value in range(2, 2 + count)
    ]


def _run(tmp_path, workers):
    source = tmp_path / "calc.py"
    source.write_bytes(b"def add():\n    return 1\n")
    log = tmp_path.parent / f"{tmp_path.name}-jobs.log"
    command = (f'"{sys.executable}" -c "import os; '
               f"open({str(log)!r}, 'a').write(os.environ.get('CARGO_BUILD_JOBS', '-') + '\\n')\"")
    outcomes = {}
    run_mutants(tmp_path, source, source.read_bytes(), _sites(source, 6), workers, CommandRunner(),
                command, tmp_path, 10, "calc.py", outcomes)
    assert len(outcomes) == 6
    return log.read_text(encoding="utf-8").split()


def test_each_worker_command_gets_its_share_of_the_cores(tmp_path, monkeypatch):
    monkeypatch.delenv("CARGO_BUILD_JOBS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    values = _run(tmp_path, 3)
    # Three pristine checks and six mutants, all inside a worker.
    assert len(values) == 9
    assert set(values) == {"2"}
    assert int(values[0]) * 3 <= 8


def test_a_single_core_still_gets_one_job(tmp_path, monkeypatch):
    monkeypatch.delenv("CARGO_BUILD_JOBS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    assert set(_run(tmp_path, 3)) == {"1"}


def test_a_user_set_job_count_is_passed_through(tmp_path, monkeypatch):
    monkeypatch.setenv("CARGO_BUILD_JOBS", "7")
    assert set(_run(tmp_path, 3)) == {"7"}


def test_the_baseline_outside_a_worker_keeps_cargos_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CARGO_BUILD_JOBS", raising=False)
    command = [sys.executable, "-c", "import os; print(os.environ.get('CARGO_BUILD_JOBS', '-'))"]
    assert CommandRunner().run(command, tmp_path, 5).output.strip() == "-"
