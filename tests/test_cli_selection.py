"""CLI routing and the conservative no-work path."""

import sys

from mutator.cli import _prepare_coverage, parse_args, run
from mutator.crapper_link import ensure_crapper


def test_full_suite_and_timings_are_explicit_cli_options():
    options = parse_args(["--full-suite", "--timings"])
    assert options.action == "mutate"
    assert options.full_suite
    assert options.timings
    assert not options.select_specs
    assert parse_args(["--select-specs"]).select_specs
    assert parse_args(["--select-specs", "--full-suite"]).exit_code == 1


def test_unchanged_killed_mutants_skip_coverage_without_rewriting_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "calc.py"
    source.write_text("def add(x):\n    return x + 1\n", encoding="utf-8")
    command = f'"{sys.executable}" -c "from calc import add; assert add(2) == 3"'
    assert run(["--root", str(tmp_path), "--no-coverage", "--test-command", command]) == 0
    snapshots = {path: path.read_bytes() for path in (tmp_path / ".metrics").rglob("*.edn")}
    assert snapshots
    crapper = ensure_crapper()
    monkeypatch.setattr(crapper.runners, "run_coverage", lambda *args: (_ for _ in ()).throw(
        AssertionError("empty differential selection must not run coverage")))
    assert run(["--root", str(tmp_path)]) == 0
    assert {path: path.read_bytes() for path in snapshots} == snapshots


def test_rewritten_function_still_requests_fresh_coverage(tmp_path, monkeypatch):
    source = tmp_path / "calc.py"
    source.write_text("def add(x):\n    return x + 1\n", encoding="utf-8")
    command = f'"{sys.executable}" -c "from calc import add; assert add(2) == 3"'
    assert run(["--root", str(tmp_path), "--no-coverage", "--test-command", command]) == 0
    source.write_text("def add(x):\n    return x + 0\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(ensure_crapper().runners, "run_coverage", lambda *args: calls.append(args))
    run(["--root", str(tmp_path)])
    assert len(calls) == 1


def test_removed_surviving_function_is_removed_from_snapshot(tmp_path):
    from mutator.edn import loads

    source = tmp_path / "calc.py"
    source.write_text("def keep(x):\n    return x + 1\n\ndef removed(x):\n    return x + 1\n", encoding="utf-8")
    command = f'"{sys.executable}" -c "from calc import keep; assert keep(2) == 3"'
    assert run(["--root", str(tmp_path), "--no-coverage", "--test-command", command]) == 3
    source.write_text("def keep(x):\n    return x + 1\n", encoding="utf-8")
    assert run(["--root", str(tmp_path), "--no-coverage", "--test-command", command]) == 0
    snapshot = loads((tmp_path / ".metrics/mutate/calc.edn").read_text(encoding="utf-8"))
    assert [form["id"] for form in snapshot["forms"]] == ["defn/keep"]
    assert set(snapshot["outcomes"].values()) == {"killed"}


def test_custom_commands_keep_standard_coverage_path(tmp_path, monkeypatch):
    source = tmp_path / "calc.lua"
    source.write_text("return {}\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(ensure_crapper().runners, "run_coverage", lambda *args: calls.append(args))
    for flags in (["--test-command", "custom tests"], ["--coverage-command", "custom coverage"],
                  ["--select-specs", "--test-command", "custom tests"],
                  ["--select-specs", "--coverage-command", "custom coverage"]):
        assert _prepare_coverage(parse_args(flags), tmp_path, [source]) == {}
    assert len(calls) == 4


def test_standard_lua_uses_the_combined_collector(tmp_path, monkeypatch):
    from mutator import lua_coverage

    source = tmp_path / "calc.lua"
    source.write_text("return {}\n", encoding="utf-8")
    runners = ensure_crapper().runners
    monkeypatch.setattr(runners, "lua_modules", lambda root, files: {tmp_path: files} if files else {})
    marker = object()
    calls = []
    monkeypatch.setattr(lua_coverage, "collect", lambda *args, **kwargs: calls.append(kwargs) or marker)
    monkeypatch.setattr(runners, "run_coverage", lambda *args: (_ for _ in ()).throw(
        AssertionError("standard Lua uses its combined collector")))
    other = tmp_path / "unused.lua"
    other.write_text("return {}\n", encoding="utf-8")
    for flags in ([], ["--full-suite"], ["--select-specs"]):
        assert _prepare_coverage(parse_args(flags), tmp_path, [source, other]) == {tmp_path: marker}
    assert len(calls) == 3
    assert calls[0]["source_files"] == [source, other]
