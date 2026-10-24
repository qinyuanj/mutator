"""A Rust mutant that allocates without bound is killed at a resident cap.

A mutated loop in a viewer test grew to 12.8 GB before its timeout and
exhausted a 15 GiB host while two other workers built. The pristine suite
peaks near 135 MB.
"""

import sys
import time

from mutator.runner import CommandRunner

# Touches every page so the allocation is resident, then waits.
GROW = (
    "import time\n"
    "block = bytearray({megabytes} * 1024 * 1024)\n"
    "for i in range(0, len(block), 4096):\n"
    "    block[i] = 1\n"
    "print('allocated', flush=True)\n"
    "time.sleep({sleep})\n"
)


def _slot_worker(tmp_path):
    worker = tmp_path / "target" / "mutation-workers" / "cargo-slot-0" / "worker"
    worker.mkdir(parents=True)
    return worker


def _grow(megabytes, sleep=30):
    return [sys.executable, "-c", GROW.format(megabytes=megabytes, sleep=sleep)]


def test_a_slot_test_run_over_the_cap_is_killed_long_before_its_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("MUTATOR_RUST_MEMORY_MB", "64")
    started = time.monotonic()
    result = CommandRunner().run(_grow(256), _slot_worker(tmp_path), 30)
    assert time.monotonic() - started < 10
    assert result.timed_out is False
    assert result.code != 0
    assert "exceeded 64 MiB" in result.output


def test_a_slot_test_run_under_the_cap_keeps_its_code(tmp_path, monkeypatch):
    monkeypatch.setenv("MUTATOR_RUST_MEMORY_MB", "512")
    result = CommandRunner().run(_grow(32, sleep=0.5), _slot_worker(tmp_path), 30)
    assert result.code == 0
    assert "exceeded" not in result.output


def test_zero_removes_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("MUTATOR_RUST_MEMORY_MB", "0")
    result = CommandRunner().run(_grow(128, sleep=0.5), _slot_worker(tmp_path), 30)
    assert result.code == 0


def test_an_untimed_build_is_not_capped(tmp_path, monkeypatch):
    # The cargo build before the timed run legitimately peaks above a test.
    monkeypatch.setenv("MUTATOR_RUST_MEMORY_MB", "64")
    result = CommandRunner().run(_grow(128, sleep=0.5), _slot_worker(tmp_path), None)
    assert result.code == 0


def test_a_run_outside_a_rust_slot_is_not_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("MUTATOR_RUST_MEMORY_MB", "64")
    result = CommandRunner().run(_grow(128, sleep=0.5), tmp_path, 30)
    assert result.code == 0
