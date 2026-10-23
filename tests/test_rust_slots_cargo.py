"""Worker slots with a real Cargo workspace (issue #20).

A workspace with ``app`` and ``util``, and ``extdep`` outside the project as a
stand-in for a registry crate. ``app``'s tests call ``util``, so a stale
mutant build of ``util`` would fail them.
"""

import os
import re
import shutil
from pathlib import Path

import pytest

from mutator.engine import mutate_file
from mutator.runner import CommandRunner
from mutator import workers
from mutator.workers import WorkerSlots

pytestmark = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo unavailable")

FILES = {
    "Cargo.toml": '[workspace]\nmembers = ["app", "util"]\nresolver = "2"\n',
    "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\nedition = "2021"\n',
    "util/src/lib.rs": "pub fn below(x: i64) -> bool {\n    x < 10\n}\n",
    "util/tests/below.rs": "#[test]\nfn below() {\n    assert!(util::below(9));\n"
                           "    assert!(!util::below(10));\n}\n",
    "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n\n'
                      '[dependencies]\nutil = { path = "../util" }\nextdep = { path = "EXTDEP" }\n',
    "app/src/lib.rs": "pub mod more;\n\npub fn twice(x: i64) -> i64 {\n    x + x\n}\n",
    "app/src/more.rs": "pub fn half(x: i64) -> i64 {\n    x / 2\n}\n",
    "app/tests/app.rs": "#[test]\nfn app() {\n    assert_eq!(app::twice(3), 6);\n"
                        "    assert_eq!(app::more::half(8), 4);\n"
                        "    assert_eq!(extdep::id(1), 1);\n"
                        "    assert!(util::below(9) && !util::below(10));\n}\n",
}


class Recording(CommandRunner):
    def __init__(self):
        super().__init__()
        self.builds = []

    def run(self, command, cwd, timeout):
        result = super().run(command, cwd, timeout)
        if "--no-run" in command:
            self.builds.append((cwd, result.output))
        return result


def workspace(tmp_path: Path) -> Path:
    extdep = tmp_path / "extdep"
    (extdep / "src").mkdir(parents=True)
    (extdep / "Cargo.toml").write_text(
        '[package]\nname = "extdep"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
    (extdep / "src" / "lib.rs").write_text("pub fn id(x: i64) -> i64 {\n    x\n}\n", encoding="utf-8")
    root = tmp_path / "project"
    for name, text in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.replace("EXTDEP", extdep.as_posix()), encoding="utf-8")
    return root


def mutate(root, relative, runner, slots):
    result = mutate_file(
        root / relative, root, runner=runner, covered_lines=None, ignore_coverage=True,
        mutate_all=True, lines=None, test_command=None, timeout_factor=20, mutation_warning=50,
        baselines={}, max_workers=1, worker_pool=slots,
    )
    assert result.baseline_failed is False, result.baseline_message
    return {(site.line, site.original, site.mutant): result.statuses[site.mutation_id]
            for site in result.sites}


def compiled(runner, start=0):
    return {name for _cwd, output in runner.builds[start:]
            for name in re.findall(r"Compiling (\S+) v", output)}


def test_slots_rebuild_only_the_mutated_crate_and_never_reuse_a_mutant_build(tmp_path):
    root = workspace(tmp_path)
    files = ["util/src/lib.rs", "app/src/lib.rs", "app/src/more.rs"]
    expected = {relative: mutate(root, relative, CommandRunner(), None) for relative in files}
    assert "killed" in expected["util/src/lib.rs"].values()
    runner = Recording()
    with WorkerSlots(root) as slots:
        # util's last mutant build stays in the slot target; app must not see it.
        assert mutate(root, files[0], runner, slots) == expected[files[0]]
        assert mutate(root, files[1], runner, slots) == expected[files[1]]
        second_file = len(runner.builds)
        assert mutate(root, files[2], runner, slots) == expected[files[2]]
        assert compiled(runner, second_file) == {"app"}
    second_run = len(runner.builds)
    with WorkerSlots(root) as slots:
        assert mutate(root, files[1], runner, slots) == expected[files[1]]
    assert compiled(runner, second_run) == {"app"}
    assert {Path(cwd).relative_to(root / "target" / "mutation-workers").parts[0]
            for cwd, _output in runner.builds if "mutation-workers" in Path(cwd).parts} == {"cargo-slot-0"}
    for relative in files:
        assert (root / relative).read_text(encoding="utf-8") == FILES[relative]


def test_a_mutant_written_in_the_tick_of_the_last_build_is_still_rebuilt(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    expected = mutate(root, "util/src/lib.rs", CommandRunner(), None)
    write_bytes = Path.write_bytes

    def same_tick(path, data):
        written = write_bytes(path, data)
        slot = next((parent for parent in path.parents if parent.name.startswith("cargo-slot-")), None)
        stamps = [] if slot is None else list(
            (slot / "target").glob("debug/.fingerprint/util-*/invoked.timestamp"))
        if path.suffix == ".rs" and stamps:
            # The kernel's write stamp equals the stamp of util's last build.
            last = min(stamp.stat().st_mtime_ns for stamp in stamps)
            os.utime(path, ns=(last, last))
        return written

    monkeypatch.setattr(Path, "write_bytes", same_tick)
    with WorkerSlots(root) as slots:
        assert mutate(root, "util/src/lib.rs", CommandRunner(), slots) == expected
    assert "killed" in expected.values()


def test_slot_targets_survive_the_run_and_overlays_do_not(tmp_path):
    root = workspace(tmp_path)
    with WorkerSlots(root) as slots:
        mutate(root, "app/src/more.rs", CommandRunner(), slots)
    slot = root / "target" / "mutation-workers" / "cargo-slot-0"
    assert (slot / "target" / "debug").is_dir()
    assert not (slot / "worker").exists()
    assert not workers._held_slots
