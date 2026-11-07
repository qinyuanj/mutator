"""A command that exits leaves nothing behind in its process group (issue #22).

On Windows the group is the command's Job Object; its leftovers are ended as
soon as the command exits, since nothing can ask them to stop first.
"""

import sys
import time
from pathlib import Path

import pytest

from host import WINDOWS, alive, shell_command
from mutator import runner
from mutator.runner import CommandResult, CommandRunner

# Starts a child that outlives it, records the child's pid, then exits.
LEAVE = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],\n"
    "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "print('done', flush=True)\n"
    "sys.exit(int(sys.argv[2]))\n"
)


def _gone_soon(pid: int, seconds: float = 2.0) -> bool:
    # The reparented child is reaped by init, which may lag a moment.
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.02)
    return not alive(pid)


def _left_behind(tmp_path: Path, code: int, trap: str = "") -> tuple[CommandResult, int]:
    pidfile = tmp_path / "child.pid"
    if trap:
        # A child that ignores SIGTERM: a POSIX shell's trap.
        command = (
            f"sh -c '{trap}sleep 600' >/dev/null 2>&1 & echo $! > {pidfile}; "
            f"echo done; exit {code}"
        )
    else:
        script = tmp_path / "leave.py"
        script.write_text(LEAVE, encoding="utf-8")
        command = shell_command([sys.executable, str(script), str(pidfile), str(code)])
    result = CommandRunner().run(command, tmp_path, 30)
    return result, int(pidfile.read_text().strip())


def test_a_child_left_by_a_passing_command_is_reaped(tmp_path):
    result, child = _left_behind(tmp_path, 0)
    assert result.code == 0
    assert result.timed_out is False
    assert "done" in result.output
    assert _gone_soon(child), f"child {child} outlived the command"


@pytest.mark.skipif(not WINDOWS, reason="Windows job completion must not wait for an inherited pipe")
def test_a_child_holding_stdout_dies_when_its_leader_exits(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "leave.py"
    script.write_text(LEAVE.replace("stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL", "stdout=None, stderr=None"),
                      encoding="utf-8")
    result = CommandRunner().run([sys.executable, str(script), str(pidfile), "3"], tmp_path, 10)
    assert result.code == 3 and not result.timed_out
    assert result.seconds < 5
    assert "done" in result.output
    assert _gone_soon(int(pidfile.read_text()))


def test_a_failing_command_keeps_its_code_and_loses_its_child(tmp_path):
    result, child = _left_behind(tmp_path, 3)
    assert result.code == 3
    assert result.timed_out is False
    assert _gone_soon(child)


@pytest.mark.skipif(WINDOWS, reason="SIGTERM and its grace are POSIX; a Windows job ends leftovers at once")
def test_a_child_that_ignores_sigterm_is_killed_after_the_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REAP_GRACE", 0.3)
    result, child = _left_behind(tmp_path, 0, trap="trap \"\" TERM; ")
    assert result.code == 0
    assert _gone_soon(child), f"child {child} survived SIGKILL"


def test_the_reap_time_is_not_charged_to_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REAP_GRACE", 1.0)
    result, child = _left_behind(tmp_path, 0, trap="" if WINDOWS else "trap \"\" TERM; ")
    assert result.seconds < 0.8
    assert _gone_soon(child)


def test_a_command_that_leaves_nothing_does_not_wait_out_the_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REAP_GRACE", 5.0)
    started = time.monotonic()
    result = CommandRunner().run("echo quiet", tmp_path, 30)
    assert result.code == 0
    assert result.output == "quiet\n"  # text mode folds cmd.exe's CRLF
    assert time.monotonic() - started < 1.0
