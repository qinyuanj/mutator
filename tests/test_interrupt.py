"""An interrupted run leaves no tests, overlays or mutants behind (issue #21)."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ORIGINAL = b"def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


def _wait(predicate, seconds=30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_an_interrupted_run_kills_its_tests_and_removes_its_overlays(tmp_path, signum):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "calc.py"
    source.write_bytes(ORIGINAL)
    pids = tmp_path / "pids"
    check = tmp_path / "check.py"
    check.write_text(
        "import os, sys, time\n"
        "if open('calc.py').read() == " + repr(ORIGINAL.decode()) + ":\n"
        "    sys.exit(0)\n"
        f"open({str(pids)!r}, 'a').write(f'{{os.getpid()}}\\n')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{check}"'
    process = subprocess.Popen(
        [sys.executable, "-m", "mutator", "--no-coverage", "--mutate-all", "--max-workers", "2",
         "--test-command", command, str(source)],
        cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert _wait(lambda: pids.exists() and len(pids.read_text().split()) >= 2), \
            process.stderr.read() if process.poll() is not None else "no mutant started"
        process.send_signal(signum)
        _out, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 128 + signum, err
    assert "Interrupted" in err
    for pid in map(int, pids.read_text().split()):
        assert _wait(lambda: not _alive(pid), 5), f"test process {pid} survived"
    assert source.read_bytes() == ORIGINAL
    assert not list((project / "target" / "mutation-workers").glob("run-*"))
    assert not (project / ".metrics").exists()
