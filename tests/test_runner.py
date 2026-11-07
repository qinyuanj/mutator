import subprocess
import sys
from pathlib import Path

import pytest

from host import VENV_PYTHON, symlink_file
from mutator.runner import CommandRunner, _clojure_command, _python_command, nearest
from mutator.runner import test_plan as plan_command


def test_nearest_walks_up_to_the_marker(tmp_path):
    root = tmp_path / "proj"
    nested = root / "src" / "demo"
    nested.mkdir(parents=True)
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    source = nested / "app.py"
    source.write_text("x = 1\n", encoding="utf-8")
    assert nearest(source, "pyproject.toml", root) == root
    assert nearest(nested, "pyproject.toml", root) == root
    assert nearest(source, "missing.toml", root) is None


def test_clojure_commands(tmp_path):
    bb = tmp_path / "bb-spec"
    bb.mkdir()
    (bb / "bb.edn").write_text('{:tasks {spec "spec"}}', encoding="utf-8")
    assert _clojure_command(bb) == ["bb", "spec", "--tag", "~no-mutate"]

    bb_test = tmp_path / "bb-test"
    bb_test.mkdir()
    (bb_test / "bb.edn").write_text("{:tasks {test (clojure \"-M:test\")}}", encoding="utf-8")
    assert _clojure_command(bb_test) == ["bb", "test"]

    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "deps.edn").write_text(
        "{:aliases {:spec {:extra-deps {speclj/speclj {}}}}}", encoding="utf-8"
    )
    assert _clojure_command(spec) == ["clj", "-M:spec", "--tag", "~no-mutate"]

    spec_only = tmp_path / "spec-only"
    spec_only.mkdir()
    (spec_only / "deps.edn").write_text("{:aliases {:spec {}}}", encoding="utf-8")
    assert _clojure_command(spec_only) == ["clj", "-M:spec"]

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "deps.edn").write_text("{:deps {}}", encoding="utf-8")
    assert _clojure_command(plain) == ["clj", "-M:test"]


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)


def test_python_command_uses_an_absolute_project_interpreter(tmp_path):
    project = tmp_path / "proj"
    dot = project / ".venv" / VENV_PYTHON
    _executable(dot)
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert _python_command(project) == [str(dot.absolute()), "-m", "pytest"]

    other = tmp_path / "other"
    plain = other / "venv" / VENV_PYTHON
    _executable(plain)
    assert _python_command(other) == [str(plain.absolute()), "-m", "unittest", "discover"]

    _executable(other / ".venv" / VENV_PYTHON)
    assert _python_command(other) == [
        str((other / ".venv" / VENV_PYTHON).absolute()),
        "-m",
        "unittest",
        "discover",
    ]


def test_python_command_keeps_the_virtualenv_symlink(tmp_path):
    project = tmp_path / "proj"
    target = project / "real-python"
    _executable(target)
    link = project / ".venv" / VENV_PYTHON
    link.parent.mkdir(parents=True)
    symlink_file(link, target)
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert _python_command(project) == [str(link.absolute()), "-m", "pytest"]


def test_python_command_falls_back_when_the_virtualenv_cannot_run(tmp_path):
    project = tmp_path / "proj"
    binary = project / ".venv" / "bin" / "python"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    assert _python_command(project) == [sys.executable, "-m", "unittest", "discover"]

    missing = tmp_path / "missing"
    missing.mkdir()
    assert _python_command(missing) == [sys.executable, "-m", "unittest", "discover"]


def test_python_commands(tmp_path):
    pytest_dir = tmp_path / "py"
    pytest_dir.mkdir()
    (pytest_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert _python_command(pytest_dir)[-2:] == ["-m", "pytest"]

    unit = tmp_path / "unit"
    unit.mkdir()
    assert _python_command(unit)[-3:] == ["-m", "unittest", "discover"]

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "setup.cfg").write_text("[tool:pytest]\n", encoding="utf-8")
    assert _python_command(cfg)[-2:] == ["-m", "pytest"]

    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\ndependencies=['pytest']\n", encoding="utf-8")
    assert _python_command(project)[-2:] == ["-m", "pytest"]

    other = tmp_path / "other"
    other.mkdir()
    (other / "setup.cfg").write_text("[metadata]\nname = demo\n", encoding="utf-8")
    assert _python_command(other)[-3:] == ["-m", "unittest", "discover"]

    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "conftest.py").write_text("", encoding="utf-8")
    assert _python_command(conf)[-2:] == ["-m", "pytest"]

    nested = tmp_path / "nested"
    (nested / "tests").mkdir(parents=True)
    (nested / "tests" / "conftest.py").write_text("", encoding="utf-8")
    assert _python_command(nested)[-2:] == ["-m", "pytest"]


def test_a_baseline_run_uses_a_dependency_installed_only_in_the_project_venv(tmp_path):
    """Issue #3 AC5: the baseline runs on the project venv's interpreter.

    The dependency exists only in the project venv's site-packages, so the
    run fails unless test_plan picks that venv's interpreter.
    """

    project = tmp_path / "proj"
    project.mkdir()
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"python -m venv failed: {created.stderr.strip()}")
    interpreter = project / ".venv" / VENV_PYTHON
    located = subprocess.run(
        [str(interpreter), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True,
        text=True,
        check=True,
    )
    site_packages = Path(located.stdout.strip())
    site_packages.mkdir(parents=True, exist_ok=True)
    (site_packages / "venv_only_dep.py").write_text("VALUE = 41\n", encoding="utf-8")
    (project / "test_dep.py").write_text(
        "import unittest\n"
        "import venv_only_dep\n\n"
        "class DepTest(unittest.TestCase):\n"
        "    def test_the_project_venv_dependency(self):\n"
        "        self.assertEqual(venv_only_dep.VALUE, 41)\n",
        encoding="utf-8",
    )
    command, directory = plan_command(project, project / "test_dep.py", "python", None)
    result = CommandRunner().run(command, directory, 120)
    assert result.code == 0, result.output
    assert "OK" in result.output

    outside = CommandRunner().run([sys.executable, "-m", "unittest", "discover"], directory, 120)
    assert outside.code != 0  # mutator's own interpreter lacks the dependency


def test_a_go_package_path_is_one_argument(tmp_path):
    root = tmp_path / "proj"
    package = root / "a;echo no"
    package.mkdir(parents=True)
    (root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
    source = package / "widget.go"
    source.write_text("package main\nfunc Run() int { return 1 }\n", encoding="utf-8")
    command, directory = plan_command(root, source, "go", None)
    assert command == ["go", "test", "-count=1", "./a;echo no"]
    assert directory == root
    echoed = CommandRunner().run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", command[-1]], directory, None
    )
    assert echoed.code == 0
    assert echoed.output.strip() == "./a;echo no"

    spaced = root / "my dir" / "widget.go"
    spaced.parent.mkdir()
    spaced.write_text("package main\nfunc Run() int { return 1 }\n", encoding="utf-8")
    command, _directory = plan_command(root, spaced, "go", None)
    assert command == ["go", "test", "-count=1", "./my dir"]
    override, override_dir = plan_command(root, source, "go", "go test ./...")
    assert override == "go test ./..."
    assert override_dir == root.resolve()


def test_a_list_command_does_not_go_through_a_shell(tmp_path):
    marker = tmp_path / "created.txt"
    code = (
        "import pathlib, sys; "
        f"pathlib.Path({str(marker)!r}).write_text(sys.argv[1], encoding='utf-8')"
    )
    result = CommandRunner().run([sys.executable, "-c", code, "no > shell"], tmp_path, None)
    assert result.code == 0
    assert marker.read_text(encoding="utf-8") == "no > shell"


def test_a_string_command_keeps_shell_semantics(tmp_path):
    marker = tmp_path / "redirected.txt"
    result = CommandRunner().run(f"echo hello > {marker}", tmp_path, None)
    assert result.code == 0
    assert marker.read_text(encoding="utf-8").strip() == "hello"


def test_a_missing_program_reports_127(tmp_path):
    result = CommandRunner().run(["no-such-mutator-program-xyz"], tmp_path, None)
    assert result.code == 127
    assert result.timed_out is False


def test_worker_lua_paths_prefer_private_sources_and_keep_project_cache(tmp_path, monkeypatch):
    import json

    root = tmp_path / "project with spaces"
    worker = root / "target" / "mutation-workers" / "run-1" / "worker-0"
    worker.mkdir(parents=True)
    for name in ("LUA_PATH", "LUA_PATH_5_4", "LUA_CPATH", "LUA_CPATH_5_4"):
        monkeypatch.setenv(name, ";".join([
            root.as_posix() + "/?.lua",
            str(root / "src" / "?.lua"),
            root.as_posix() + "/.toolcache/luarocks/?.lua",
            root.as_posix() + "-other/?.lua",
            "./?.lua", "", "external/?.lua",
        ]))
    names = ["LUA_PATH", "LUA_PATH_5_4", "LUA_CPATH", "LUA_CPATH_5_4",
             "MUTATOR_WORKER_ROOT", "MUTATOR_SOURCE_ROOT"]
    command = [sys.executable, "-c",
               "import json, os, sys; print(json.dumps({k: os.getenv(k) for k in sys.argv[1:]}))",
               *names]
    result = CommandRunner().run(command, worker, 5)
    assert result.code == 0, result.output
    environment = json.loads(result.output)
    expected = ";".join([
        worker.as_posix() + "/?.lua", worker.as_posix() + "/src/?.lua",
        root.as_posix() + "/.toolcache/luarocks/?.lua",
        root.as_posix() + "-other/?.lua", "./?.lua", "", "external/?.lua",
    ])
    for name in names[:4]:
        assert environment[name] == expected
    assert Path(environment["MUTATOR_WORKER_ROOT"]) == worker
    assert Path(environment["MUTATOR_SOURCE_ROOT"]) == root
    # The baseline outside a worker cannot inherit stale worker credentials.
    monkeypatch.setenv("MUTATOR_WORKER_ROOT", "stale")
    monkeypatch.setenv("MUTATOR_SOURCE_ROOT", "stale")
    baseline = CommandRunner().run(command, root, 5)
    environment = json.loads(baseline.output)
    assert environment["MUTATOR_WORKER_ROOT"] is None
    assert environment["MUTATOR_SOURCE_ROOT"] is None


def test_a_worker_overlay_is_imported_ahead_of_the_environment(tmp_path):
    worker = tmp_path / "target" / "mutation-workers" / "run-1" / "worker-0"
    (worker / "src").mkdir(parents=True)
    (worker / "src" / "demo.py").write_text("VALUE = 'worker'\n", encoding="utf-8")
    command = [sys.executable, "-c", "import demo; print(demo.VALUE)"]
    result = CommandRunner().run(command, worker, 5)
    assert result.code == 0
    assert result.output.strip() == "worker"


def test_plans_fall_back_to_the_root_and_honor_an_override(tmp_path):
    root = tmp_path.resolve()
    source = root / "src" / "a.txt"
    source.parent.mkdir()
    source.write_text("x\n", encoding="utf-8")
    assert plan_command(root, source, "java", None) == (["mvn", "-q", "test", "-DexcludeTags=no-mutate"], root)
    assert plan_command(root, source, "typescript", None) == (["npm", "test"], root)
    assert plan_command(root, source, "rust", None) == (["cargo", "test"], root)
    assert plan_command(root, source, "go", None) == (["go", "test", "-count=1", "./src"], root)
    assert plan_command(root, source, "clojure", None) == (["clj", "-M:test"], root)
    assert plan_command(root, source, "python", None) == (_python_command(root), root)
    assert plan_command(root, source, "cobol", None) == (["false"], root)
    assert plan_command(root, source, "java", "make check") == ("make check", root)
    (root / "go.mod").write_text("module demo\n", encoding="utf-8")
    assert plan_command(root, root / "main.go", "go", None) == (["go", "test", "-count=1", "."], root)


def test_plans_prefer_markers_in_a_fixed_order_over_nearness(tmp_path):
    root = tmp_path.resolve()
    sub = root / "sub"
    sub.mkdir()
    source = sub / "app.py"
    source.write_text("x = 1\n", encoding="utf-8")
    (sub / "setup.cfg").write_text("[metadata]\n", encoding="utf-8")
    assert plan_command(root, source, "python", None)[1] == sub
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert plan_command(root, source, "python", None)[1] == root
    (sub / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert plan_command(root, source, "python", None)[1] == root

    clj = sub / "core.clj"
    clj.write_text("(ns core)\n", encoding="utf-8")
    (sub / "bb.edn").write_text("{}\n", encoding="utf-8")
    assert plan_command(root, clj, "clojure", None) == (["bb", "test"], sub)
    (root / "deps.edn").write_text("{}\n", encoding="utf-8")
    assert plan_command(root, clj, "clojure", None) == (["clj", "-M:test"], root)


@pytest.mark.skipif(sys.platform == "win32", reason="process-group signals are POSIX; Windows uses a job")
def test_a_timeout_survives_a_process_group_that_cannot_be_signalled(tmp_path, monkeypatch):
    def refuse(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("mutator.runner.os.killpg", refuse)
    # An argv, so the leader is the sleeper itself: a shell that does not exec
    # its last command (dash here) would leave it holding the output pipe.
    result = CommandRunner().run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 0.5)
    assert (result.code, result.timed_out) == (124, True)
    assert result.seconds < 10


def test_output_that_is_not_utf8_still_yields_a_result(tmp_path):
    # A mutant can cut a multibyte string in half; the test output then holds
    # bytes that are not UTF-8, and the outcome must still be recorded.
    result = CommandRunner().run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok \\x82 end'); sys.exit(3)"],
        tmp_path, 30)
    assert result.code == 3
    assert result.output.startswith("ok ") and result.output.endswith(" end")
