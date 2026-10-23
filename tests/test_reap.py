"""A command that exits leaves nothing behind in its process group (issue #22)."""

import time
from pathlib import Path

import pytest

from mutator import runner
from mutator.runner import CommandResult, CommandRunner


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


def _gone_soon(pid: int, seconds: float = 2.0) -> bool:
    # The reparented child is reaped by init, which may lag a moment.
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return not _alive(pid)


def _left_behind(tmp_path: Path, code: int, trap: str = "") -> tuple[CommandResult, int]:
    pidfile = tmp_path / "child.pid"
    command = (
        f"sh -c '{trap}sleep 600' >/dev/null 2>&1 & echo $! > {pidfile}; "
        f"echo done; exit {code}"
    )
    result = CommandRunner().run(command, tmp_path, 30)
    return result, int(pidfile.read_text().strip())


def test_a_child_left_by_a_passing_command_is_reaped(tmp_path):
    result, child = _left_behind(tmp_path, 0)
    assert result.code == 0
    assert result.timed_out is False
    assert "done" in result.output
    assert _gone_soon(child), f"child {child} outlived the command"


def test_a_failing_command_keeps_its_code_and_loses_its_child(tmp_path):
    result, child = _left_behind(tmp_path, 3)
    assert result.code == 3
    assert result.timed_out is False
    assert _gone_soon(child)


def test_a_child_that_ignores_sigterm_is_killed_after_the_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REAP_GRACE", 0.3)
    result, child = _left_behind(tmp_path, 0, trap="trap \"\" TERM; ")
    assert result.code == 0
    assert _gone_soon(child), f"child {child} survived SIGKILL"


def test_the_reap_time_is_not_charged_to_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REAP_GRACE", 1.0)
    result, child = _left_behind(tmp_path, 0, trap="trap \"\" TERM; ")
    assert result.seconds < 0.8
    assert _gone_soon(child)


def test_a_command_that_leaves_nothing_does_not_wait_out_the_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REAP_GRACE", 5.0)
    started = time.monotonic()
    result = CommandRunner().run("echo quiet", tmp_path, 30)
    assert result.code == 0
    assert result.output == "quiet\n"
    assert time.monotonic() - started < 1.0


def test_undecodable_output_still_raises(tmp_path):
    with pytest.raises(UnicodeDecodeError):
        CommandRunner().run("printf '\\377'", tmp_path, 30)
