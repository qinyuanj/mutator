"""Timeouts kill the whole process tree on every platform (issue #2)."""

import os
import sys
import time
from pathlib import Path

from mutator.runner import CommandRunner

SLEEPING_CHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _dead(pid: int, grace: float = 5.0) -> bool:
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.05)
    return not _process_alive(pid)


def test_a_timeout_returns_the_result_with_partial_output(tmp_path):
    marker = "partial-before-sleep"
    command = f'"{sys.executable}" -c "import time; print({marker!r}, flush=True); time.sleep(60)"'
    result = CommandRunner().run(command, tmp_path, 1.0)
    assert result.timed_out is True
    assert result.code == 124
    assert marker in result.output


def test_a_timeout_leaves_no_descendant_alive(tmp_path):
    parent = tmp_path / "parent.py"
    parent.write_text(SLEEPING_CHILD, encoding="utf-8")
    result = CommandRunner().run(f'"{sys.executable}" "{parent}"', tmp_path, 1.5)
    assert result.timed_out is True
    assert result.code == 124
    child_pid = int(result.output.strip().splitlines()[0])
    assert _dead(child_pid), f"descendant {child_pid} survived the timeout"
