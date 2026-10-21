"""Named Busted tasks through the CLI and real runner boundaries (issue #6)."""

from datetime import datetime
from pathlib import Path

from mutator.edn import loads
from mutator.cli import parse_args, run
from mutator.runner import CommandRunner
from mutator.runner import test_plan as command_for
from mutator.metrics import load_history


def task_project(tmp_path: Path) -> Path:
    project = tmp_path / "project with spaces"
    (project / "src").mkdir(parents=True)
    (project / "spec").mkdir()
    (project / "src" / "calc.lua").write_text(
        "local M = {}\nfunction M.add(x)\n  return x + 1\nend\nreturn M\n",
        encoding="utf-8", newline="\n",
    )
    (project / ".busted").write_text(
        "return { default = { lpath = 'src/?.lua', ROOT = {'spec'} },\n"
        " fast = { ['exclude-tags'] = 'slow' } }\n",
        encoding="utf-8",
    )
    (project / "spec" / "calc_spec.lua").write_text(
        "local calc = require('calc')\n"
        "describe('calc', function()\n"
        "  it('returns a number', function()\n"
        "    assert.are.equal('number', type(calc.add(2)))\n"
        "  end)\n"
        "  it('adds accurately #slow', function()\n"
        "    assert.are.equal(3, calc.add(2))\n"
        "  end)\n"
        "end)\n",
        encoding="utf-8",
    )
    return project


def test_fast_task_leaves_survivors_and_publishes_selected_results(tmp_path):
    project = task_project(tmp_path)
    args = ["--root", str(project), "--no-coverage", "--mutate-all", "--max-workers", "1"]
    assert run([*args, "--busted-task", "fast"]) == 3
    history = load_history(project, "src/calc.lua")
    assert list(history.outcomes.values()) == ["survived", "survived"]
    snapshot = project / ".metrics" / "mutate" / "calc.edn"
    assert ":killed 0 :survived 2" in snapshot.read_text(encoding="utf-8")
    assert run(args) == 0
    history = load_history(project, "src/calc.lua")
    assert list(history.outcomes.values()) == ["killed", "killed"]
    assert ":killed 2 :survived 0" in snapshot.read_text(encoding="utf-8")


def test_fast_task_publishes_survivors_without_touching_sources(tmp_path, monkeypatch):
    project = task_project(tmp_path)
    originals = {
        path: path.read_bytes() for path in project.rglob("*") if path.is_file()
    }
    monkeypatch.setattr("os.cpu_count", lambda: 2)
    assert run([
        "--root", str(project), "--no-coverage", "--mutate-all",
        "--max-workers", "2", "--busted-task", "fast",
    ]) == 3

    snapshot = project / ".metrics" / "mutate" / "calc.edn"
    published = loads(snapshot.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(published.pop("tested-at")).tzinfo is not None
    assert published["forms"][0].pop("hash")
    assert published == {
        "version": 1,
        "source": "src/calc.lua",
        "namespace": "calc",
        "outcomes": {
            '["src/calc.lua","calc","defn/M.add",42,43,"+","-"]': "survived",
            '["src/calc.lua","calc","defn/M.add",44,45,"1","0"]': "survived",
        },
        "forms": [{
            "id": "defn/M.add", "kind": "defn", "file": "src/calc.lua",
            "line": 2, "end-line": 4, "killed": 0, "survived": 2,
            "uncovered": 0, "sites": 2,
        }],
    }
    assert {path: path.read_bytes() for path in originals} == originals
    assert list((project / "target" / "mutation-workers").iterdir()) == []


def test_busted_task_is_rejected_with_an_explicit_test_command():
    options = parse_args(["--busted-task", "fast", "--test-command", "busted spec"])
    assert options.action == "help"
    assert options.exit_code == 1
    assert options.busted_task is None
    assert options.test_command is None
    assert "--busted-task cannot be combined with --test-command" in options.message


def test_busted_task_is_a_single_argv_entry_in_the_generated_command(tmp_path, monkeypatch):
    import mutator.crapper_link as link

    crapper = link.ensure_crapper()
    monkeypatch.setattr(crapper.runners, "lua_interpreter", lambda: "/opt/lua 5.4/lua")
    source = tmp_path / "a.lua"
    source.write_text("", encoding="utf-8")
    command, directory = command_for(
        tmp_path, source, "lua", None, busted_task="task with spaces"
    )
    assert command[:2] == ["/opt/lua 5.4/lua", "-e"]
    assert command[2].endswith("require('busted.runner')({standalone = false}); os.exit(0)")
    assert command[3:] == ["--", "busted", "--ignore-lua", "--run", "task with spaces"]
    assert directory == tmp_path.resolve()
    explicit, explicit_dir = command_for(
        tmp_path, source, "lua", "busted --run 'task with spaces'"
    )
    assert explicit == "busted --run 'task with spaces'"
    assert explicit_dir == tmp_path.resolve()


def test_fast_task_excludes_a_failing_slow_test_from_baseline_and_mutants(tmp_path):
    project = task_project(tmp_path)
    spec = project / "spec" / "calc_spec.lua"
    spec.write_text(spec.read_text(encoding="utf-8").replace(
        "assert.are.equal(3, calc.add(2))", "error('slow test was executed')"
    ), encoding="utf-8")
    command, directory = command_for(project, project / "src" / "calc.lua", "lua", None)
    failed = CommandRunner().run(command, directory, 60)
    assert failed.code == 1, failed.output  # Busted's original subprocess status
    assert "slow test was executed" in failed.output
    args = ["--root", str(project), "--no-coverage", "--mutate-all", "--max-workers", "1"]
    assert run(args) == 2  # mutation CLI maps baseline failure to exit 2
    assert not (project / ".metrics" / "mutate").exists()
    assert run([*args, "--busted-task", "fast"]) == 3
    assert list(load_history(project, "src/calc.lua").outcomes.values()) == [
        "survived", "survived"
    ]


def test_explicit_command_preserves_quoted_arguments_and_original_subprocess_status(tmp_path):
    import json
    import sys

    project = task_project(tmp_path)
    script = project / "check arguments.py"
    script.write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(7)\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}" "argument with spaces" "quoted value"'
    planned, directory = command_for(project, project / "src" / "calc.lua", "lua", command)
    assert planned == command
    result = CommandRunner().run(planned, directory, 60)
    assert result.code == 7
    assert json.loads(result.output) == ["argument with spaces", "quoted value"]
    assert run(["--root", str(project), "--no-coverage", "--test-command", command]) == 2
    assert not (project / ".metrics" / "mutate").exists()
