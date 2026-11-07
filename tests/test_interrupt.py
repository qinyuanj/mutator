"""An interrupted run leaves no tests, overlays or mutants behind (issue #21).

Windows delivers neither SIGTERM nor a Ctrl+C sent to another console group,
so there the interrupt is Ctrl+Break (SIGBREAK). A mutator killed outright
leaves no test running (its jobs close with it) and its copies are swept by
the next run.
"""

import signal
import subprocess
import sys
import time

import pytest

from host import WINDOWS, alive

ORIGINAL = b"def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"

STOPS = [signal.CTRL_BREAK_EVENT] if WINDOWS else [signal.SIGTERM, signal.SIGINT]
# The signal mutator sees for each stop, and so its exit status 128 + signum.
SEEN = {getattr(signal, "CTRL_BREAK_EVENT", None): getattr(signal, "SIGBREAK", None)}


def _wait(predicate, seconds=30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _start(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "calc.py"
    source.write_bytes(ORIGINAL)
    # One file per mutant: two processes appending to one file can lose a
    # line on Windows, where append mode seeks to the end and then writes.
    pids = tmp_path / "pids"
    pids.mkdir()
    check = tmp_path / "check.py"
    check.write_text(
        "import os, sys, time\n"
        "if open('calc.py').read() == " + repr(ORIGINAL.decode()) + ":\n"
        "    sys.exit(0)\n"
        f"open(os.path.join({str(pids)!r}, str(os.getpid())), 'w').close()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{check}"'
    process = subprocess.Popen(
        [sys.executable, "-m", "mutator", "--no-coverage", "--mutate-all", "--max-workers", "2",
         "--test-command", command, str(source)],
        cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        # Ctrl+Break reaches only a process group of its own.
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0,
    )
    started = _wait(lambda: len(list(pids.iterdir())) >= 2)
    if not started:
        message = process.stderr.read() if process.poll() is not None else "no mutant started"
        process.kill()
        pytest.fail(message)
    return project, source, pids, process


@pytest.mark.parametrize("signum", STOPS)
def test_an_interrupted_run_kills_its_tests_and_removes_its_overlays(tmp_path, signum):
    project, source, pids, process = _start(tmp_path)
    try:
        process.send_signal(signum)
        _out, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 128 + SEEN.get(signum, signum), err
    assert "Interrupted" in err
    for pid in (int(entry.name) for entry in pids.iterdir()):
        assert _wait(lambda: not alive(pid), 5), f"test process {pid} survived"
    assert source.read_bytes() == ORIGINAL
    assert not list((project / "target" / "mutation-workers").glob("run-*"))
    assert not (project / ".metrics").exists()


@pytest.mark.skipif(not WINDOWS, reason="Windows baseline homes use locked, recoverable run directories")
def test_a_killed_baseline_home_is_swept_by_the_next_run(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "calc.py"
    source.write_bytes(ORIGINAL)
    marker = tmp_path / "baseline.pid"
    check = tmp_path / "baseline.py"
    check.write_text(f"import os, time\nopen({str(marker)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n",
                     encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "mutator", "--no-coverage", "--mutate-all",
         "--test-command", f'"{sys.executable}" "{check}"', str(source)],
        cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert _wait(lambda: marker.exists() and marker.read_text().strip())
        pid = int(marker.read_text())
    finally:
        process.kill()
        process.communicate(timeout=30)
    assert _wait(lambda: not alive(pid), 5)
    workers = project / "target" / "mutation-workers"
    assert list(workers.glob("run-home-*"))
    rerun = subprocess.run(
        [sys.executable, "-m", "mutator", "--no-coverage", "--mutate-all", "--max-workers", "1",
         "--test-command", f'"{sys.executable}" -c "raise SystemExit(0)"', str(source)],
        cwd=project, capture_output=True, text=True, timeout=120,
    )
    assert rerun.returncode == 3, rerun.stderr
    assert not list(workers.glob("run-*"))


@pytest.mark.skipif(not WINDOWS, reason="a SIGKILLed mutator's Linux test sessions outlive it; "
                    "its views vanish with its namespace")
def test_a_killed_mutator_leaves_no_test_running_and_the_next_run_sweeps_its_copies(tmp_path):
    project, source, pids, process = _start(tmp_path)
    process.kill()
    process.communicate(timeout=30)
    for pid in (int(entry.name) for entry in pids.iterdir()):
        assert _wait(lambda: not alive(pid), 5), f"test process {pid} outlived mutator"
    workers = project / "target" / "mutation-workers"
    abandoned = list(workers.glob("run-*"))
    assert abandoned, "a killed run leaves its copies"
    passing = f'"{sys.executable}" -c "raise SystemExit(0)"'
    rerun = subprocess.run(
        [sys.executable, "-m", "mutator", "--no-coverage", "--mutate-all", "--max-workers", "1",
         "--test-command", passing, str(source)],
        cwd=project, capture_output=True, text=True, timeout=120,
    )
    assert rerun.returncode == 3, rerun.stderr
    assert not any(directory.exists() for directory in abandoned)
    assert not list(workers.glob("run-*"))
    assert source.read_bytes() == ORIGINAL
