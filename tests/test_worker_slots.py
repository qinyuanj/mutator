"""Rust worker slots keep their overlay path and Cargo target (issue #20)."""

import json
import os
import sys
from pathlib import Path

import pytest

from host import symlink_file
from mutator.runner import CommandRunner
from mutator.model import Site
from mutator.workers import WorkerSlots, discard_overlays, run_mutants


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


def test_the_next_file_sees_the_project_copy_of_the_previous_file(tmp_path):
    a, b = project(tmp_path)
    seen = tmp_path / "seen"
    script = ("import sys; from pathlib import Path; "
              "open(sys.argv[1], 'ab').write(Path('src/a.rs').read_bytes()); "
              "raise SystemExit(Path('src/b.rs').read_bytes() != Path('src/a.rs').read_bytes())")
    command = [sys.executable, "-c", script, str(seen)]
    mutant = Site("src/a.rs", "f", "fn/f", "f", 2, 24, 25, "1", "2", "number", "two")
    with WorkerSlots(tmp_path) as slots:
        first = slots.prepare("src/a.rs", a.read_bytes(), 2)
        outcomes = {}
        run_mutants(tmp_path, a, a.read_bytes(), [mutant], 1, CommandRunner(), command,
                    tmp_path, 5, "src/a.rs", outcomes, pool=slots)
        assert outcomes == {"two": "killed"}
        second = slots.prepare("src/b.rs", b.read_bytes(), 1)
        assert second == first[:1]
        # An unused slot does not keep the previous file's worker either.
        assert not first[1].exists()
        seen.write_bytes(b"")
        mutant_b = Site("src/b.rs", "f", "fn/f", "f", 2, 24, 25, "1", "2", "number", "b2")
        run_mutants(tmp_path, b, b.read_bytes(), [mutant_b], 1, CommandRunner(), command,
                    tmp_path, 5, "src/b.rs", outcomes, pool=slots)
    assert set(seen.read_bytes().split(b"pub fn")) == {b"", b" f() -> i64 {\n    1\n}\n"}
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


def test_a_link_in_the_project_build_stays_a_link_in_the_seeded_slot(tmp_path):
    a, _b = project(tmp_path)
    debug = tmp_path / "target" / "debug"
    debug.mkdir(parents=True)
    (debug / "libdemo.rlib").write_bytes(b"rlib")
    symlink_file(debug / "demo.rlib", Path("libdemo.rlib"))
    with WorkerSlots(tmp_path) as slots:
        [worker] = slots.prepare("src/a.rs", a.read_bytes(), 1)
    seeded = worker.parent / "target" / "debug" / "demo.rlib"
    assert seeded.is_symlink()
    assert os.readlink(seeded) == "libdemo.rlib"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="cp seeds POSIX slots; root reads an unreadable file anyway")
def test_a_failed_copy_names_what_cp_said_and_the_slot_builds_cold(tmp_path, capsys):
    a, _b = project(tmp_path)
    debug = tmp_path / "target" / "debug"
    debug.mkdir(parents=True)
    unreadable = debug / "unreadable-artifact"
    unreadable.write_bytes(b"rlib")
    unreadable.chmod(0)
    try:
        with WorkerSlots(tmp_path) as slots:
            [worker] = slots.prepare("src/a.rs", a.read_bytes(), 1)
    finally:
        unreadable.chmod(0o600)
    err = capsys.readouterr().err
    assert "building cold" in err
    assert "unreadable-artifact" in err
    assert not (worker.parent / "target").exists()


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
        result = CommandRunner().run(command, worker, 5)
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
