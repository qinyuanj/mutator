"""Rust worker slots keep their overlay path and Cargo target (issue #20)."""

import json
import os
import sys

from mutator.runner import CommandRunner
from mutator.workers import WorkerSlots, _is_link, discard_overlays


def project(root):
    (root / "Cargo.toml").write_text('[package]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    (root / "src").mkdir()
    for name in ("a.rs", "b.rs"):
        (root / "src" / name).write_bytes(b"pub fn f() -> i64 {\n    1\n}\n")
    return [root / "src" / name for name in ("a.rs", "b.rs")]


def test_two_concurrent_runs_get_different_slot_numbers(tmp_path):
    a, _b = project(tmp_path)
    with WorkerSlots(tmp_path) as first, WorkerSlots(tmp_path) as second:
        [one] = first.prepare("src/a.rs", a.read_bytes(), 1)
        [two] = second.prepare("src/a.rs", a.read_bytes(), 1)
        assert one.parent.name == "cargo-slot-0"
        assert two.parent.name == "cargo-slot-1"
    with WorkerSlots(tmp_path) as later:
        # Released slots are taken again from the lowest number.
        assert later.prepare("src/a.rs", a.read_bytes(), 1)[0].parent.name == "cargo-slot-0"


def test_the_next_file_replaces_the_previous_private_copy_with_a_link(tmp_path):
    a, b = project(tmp_path)
    with WorkerSlots(tmp_path) as slots:
        first = slots.prepare("src/a.rs", a.read_bytes(), 2)
        for worker in first:
            assert not _is_link(worker / "src" / "a.rs")
            (worker / "src" / "a.rs").write_bytes(b"left behind\n")
        second = slots.prepare("src/b.rs", b.read_bytes(), 1)
        assert second == first[:1]
        assert _is_link(second[0] / "src" / "a.rs")
        assert not _is_link(second[0] / "src" / "b.rs")
        # An unused slot does not keep the previous file's overlay either.
        assert not first[1].exists()
        slots.validate()
    assert a.read_bytes() == b.read_bytes() == b"pub fn f() -> i64 {\n    1\n}\n"


def test_a_new_slot_target_is_seeded_from_the_project_build_with_its_mtimes(tmp_path):
    a, _b = project(tmp_path)
    built = tmp_path / "target" / "debug" / ".fingerprint" / "demo-0123456789abcdef" / "lib-demo"
    built.parent.mkdir(parents=True)
    built.write_text("fingerprint", encoding="utf-8")
    os.utime(built, ns=(1_000_000_000, 1_000_000_000))
    with WorkerSlots(tmp_path) as slots:
        [worker] = slots.prepare("src/a.rs", a.read_bytes(), 1)
    seeded = worker.parent / "target" / "debug" / ".fingerprint" / "demo-0123456789abcdef" / "lib-demo"
    assert seeded.read_text(encoding="utf-8") == "fingerprint"
    assert seeded.stat().st_mtime_ns == 1_000_000_000
    # An existing slot target is kept, not seeded again.
    (tmp_path / "target" / "debug" / "later").write_text("newer", encoding="utf-8")
    with WorkerSlots(tmp_path) as slots:
        slots.prepare("src/a.rs", a.read_bytes(), 1)
    assert not (worker.parent / "target" / "debug" / "later").exists()


def test_without_a_project_build_the_slot_builds_cold(tmp_path):
    a, _b = project(tmp_path)
    with WorkerSlots(tmp_path) as slots:
        [worker] = slots.prepare("src/a.rs", a.read_bytes(), 1)
    assert not (worker.parent / "target").exists()


def test_the_package_a_previous_file_mutated_is_rebuilt_in_its_slot(tmp_path):
    a, b = project(tmp_path)
    with WorkerSlots(tmp_path) as slots:
        [worker] = slots.prepare("src/a.rs", a.read_bytes(), 1)
        fingerprints = worker.parent / "target" / "debug" / ".fingerprint"
        mutated = fingerprints / "demo-0123456789abcdef"
        other = fingerprints / "demo-extra-0123456789abcdef"
        for directory in (mutated, other):
            directory.mkdir(parents=True)
        slots.prepare("src/b.rs", b.read_bytes(), 1)
        assert not mutated.exists()
        assert other.exists()


def test_commands_in_a_slot_build_into_its_slot_target(tmp_path, monkeypatch):
    a, _b = project(tmp_path)
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    command = [sys.executable, "-c",
               "import json, os; print(json.dumps([os.environ.get(k) for k in "
               "('CARGO_TARGET_DIR', 'CARGO_BUILD_BUILD_DIR')]))"]
    with WorkerSlots(tmp_path) as slots:
        [worker] = slots.prepare("src/a.rs", a.read_bytes(), 1)
        result = CommandRunner().run(command, worker / "src", 5)
    target = str(worker.parent / "target")
    assert json.loads(result.output) == [target, target]
    assert json.loads(CommandRunner().run(command, tmp_path, 5).output) == [None, None]


def test_an_interrupt_removes_slot_overlays_and_keeps_slot_targets(tmp_path):
    a, _b = project(tmp_path)
    with WorkerSlots(tmp_path) as slots:
        [worker] = slots.prepare("src/a.rs", a.read_bytes(), 1)
        (worker.parent / "target").mkdir()
        discard_overlays()
        assert not worker.exists()
        assert (worker.parent / "target").is_dir()
    assert a.read_bytes() == b"pub fn f() -> i64 {\n    1\n}\n"


def test_an_unrecognised_fingerprint_layout_discards_the_slot_target(tmp_path, capsys):
    a, b = project(tmp_path)
    with WorkerSlots(tmp_path) as slots:
        [worker] = slots.prepare("src/a.rs", a.read_bytes(), 1)
        target = worker.parent / "target"
        (target / "debug" / ".fingerprint" / "demo-0123").mkdir(parents=True)
        slots.prepare("src/b.rs", b.read_bytes(), 1)
        assert not target.exists()
    assert "discarding the slot target" in capsys.readouterr().err
