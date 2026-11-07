"""Copy isolation, the declared isolation of native Windows (ADR 0007)."""

import shutil
import sys

import pytest

from host import WINDOWS
from mutator import isolation
from mutator.isolation import CopyViews, copy_method
from mutator.model import Site
from mutator.runner import CommandRunner
from mutator.workers import run_mutants

# Each mutant writes into its working directory, the project, HOME and TEMP,
# then fails. It first checks that no earlier mutant's writes are visible.
WRITER = """
import os, sys
from pathlib import Path
sys.path.insert(0, ".")
from value import value
if value == 1:
    sys.exit(0)
seen = [p for p in (Path("leak"), Path("../leak"),
                    Path(os.environ["HOME"], "leak"), Path(os.environ["TEMP"], "leak"),
                    Path(os.environ["USERPROFILE"], "leak"), Path(os.environ["APPDATA"], "leak"),
                    Path(os.environ["LOCALAPPDATA"], "leak"))
        if p.exists()]
if seen:
    print("saw an earlier mutant's write:", seen)
    sys.exit(0)  # survives: the assertion below catches it
for folder in (".", "..", os.environ["HOME"], os.environ["TEMP"], os.environ["USERPROFILE"],
               os.environ["APPDATA"], os.environ["LOCALAPPDATA"]):
    Path(folder, "leak").write_text("x")
sys.exit(1)
"""


def _project(root):
    (root / "pkg").mkdir()
    source = root / "pkg" / "value.py"
    source.write_bytes(b"value = 1\n")
    return source


def _sites():
    return [Site("pkg/value.py", "value", "defn/value", "value", 1,
                 8, 9, "1", str(value), "number", str(value)) for value in (2, 3, 4)]


@pytest.mark.skipif(not WINDOWS, reason="Linux views keep the real home; only copies redirect it")
def test_writes_to_the_project_home_and_temp_reach_neither_the_user_nor_a_later_mutant(
        tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    source = _project(project)
    real = tmp_path / "real-home"
    for name in ("HOME", "USERPROFILE", "TEMP", "APPDATA", "LOCALAPPDATA"):
        (real / name).mkdir(parents=True)
        monkeypatch.setenv(name, str(real / name))
    outcomes = {}
    # One worker, so every mutant runs at the same path after the previous one.
    run_mutants(project, source, source.read_bytes(), _sites(), 1, CommandRunner(),
                [sys.executable, "-c", WRITER], project / "pkg", 30, "pkg/value.py", outcomes)
    assert outcomes == {"2": "killed", "3": "killed", "4": "killed"}
    assert not list(real.rglob("leak"))
    assert not list(project.rglob("leak"))
    assert source.read_bytes() == b"value = 1\n"
    assert not list((project / "target" / "mutation-workers").glob("run-*"))


@pytest.mark.skipif(not WINDOWS, reason="only native Windows redirects command homes")
def test_the_baseline_also_writes_into_a_private_home(tmp_path, monkeypatch):
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    for name in ("HOME", "USERPROFILE", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(real_home))
    script = ("import os; from pathlib import Path; "
              "[Path(os.environ[k], 'leak').write_text('x') for k in "
              "('HOME', 'USERPROFILE', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA')]")
    result = CommandRunner().run([sys.executable, "-c", script], project, 30)
    assert result.code == 0, result.output
    assert not list(real_home.iterdir())
    assert not list(project.rglob("leak"))
    assert not list((project / "target" / "mutation-homes").iterdir())


@pytest.mark.skipif(not WINDOWS, reason="the fixture deliberately calls a native Win32 API")
def test_cli_tests_windows_api_code_in_native_workers(tmp_path):
    from host import shell_command
    from mutator.cli import run

    source = tmp_path / "native.py"
    source.write_bytes(b"import ctypes\ndef next_pid():\n    return ctypes.windll.kernel32.GetCurrentProcessId() + 1\n")
    command = shell_command([sys.executable, "-c", "import os; from native import next_pid; assert next_pid() == os.getpid() + 1"])
    assert run(["--root", str(tmp_path), "--no-coverage", "--mutate-all", "--max-workers", "1",
                "--test-command", command, str(source)]) == 0
    data = (tmp_path / ".metrics" / "mutate" / "native.edn").read_bytes()
    assert b":isolation :copy" in data
    assert b":survived 0" in data


def test_the_copier_is_a_parameter_so_another_hosts_clone_call_plugs_in(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    (project / "notes.txt").write_text("notes")
    (project / "node_modules").mkdir()
    dependency = project / "node_modules" / "library.js"
    dependency.write_text("original dependency")
    (project / "build").mkdir()
    (project / "build" / "stale.txt").write_text("stale")
    copied = []

    def clone(source, destination):
        copied.append(source.name)
        shutil.copy2(source, destination)

    views = CopyViews(copy_file=clone)
    worker = tmp_path / "workers" / "worker-0"
    views.prepare(worker, project, ["pkg/value.py"], {})
    with views.view(worker, project, "pkg/value.py", b"value = 2\n"):
        assert (worker / "pkg" / "value.py").read_bytes() == b"value = 2\n"
        assert (worker / "notes.txt").read_text() == "notes"
        (worker / "node_modules" / "library.js").write_text("mutant write")
        assert dependency.read_text() == "original dependency"
        # Build output is hidden as in an overlay view.
        assert (worker / "build").is_dir() and not any((worker / "build").iterdir())
    assert sorted(copied) == ["library.js", "notes.txt", "value.py"]
    assert not any(worker.iterdir())


# Declared volumes: this host has no ReFS, so the block-clone decision is
# tested on what GetVolumeInformation would report.
@pytest.mark.parametrize("filesystem, flags, build, expected", [
    ("ReFS", 0x08000000, 26100, "block clone"),   # Dev Drive on Windows 11 24H2
    ("ReFS", 0x08000000, 22631, "full copy"),     # 23H2: CopyFile2 does not clone
    ("ReFS", 0, 26100, "full copy"),              # no block refcounting
    ("NTFS", 0x08000000, 26100, "full copy"),
    ("NTFS", 0, 19045, "full copy"),
])
def test_block_cloning_is_reported_only_where_copies_clone(filesystem, flags, build, expected):
    assert copy_method(filesystem, flags, build) == expected


@pytest.mark.skipif(not WINDOWS, reason="describes the Windows volume of the project")
def test_the_declared_isolation_names_the_copy_method_and_volume(tmp_path):
    filesystem, flags = isolation.volume(tmp_path)
    assert filesystem
    described = CopyViews().describe(tmp_path)
    assert described.startswith("copy (a fresh ")
    method = copy_method(filesystem, flags, sys.getwindowsversion().build)
    assert f"{method} of the project per command on {filesystem})" in described
