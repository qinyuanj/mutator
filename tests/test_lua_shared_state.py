"""Passing lua_coverage to mutate_file explicitly opts into spec selection.

Selection assumes independent specs. Survivor confirmation catches omitted
assertions, but pristine subset checks cannot guarantee parity for shared state.
"""

import pytest

from mutator.crapper_link import ensure_crapper
from mutator.engine import mutate_file
from mutator.lua_coverage import collect
from mutator.runner import CommandRunner


def test_shared_cached_result_keeps_full_suite_kills(tmp_path):
    runners = ensure_crapper().runners
    lua = runners.lua_interpreter()
    if lua is None:
        pytest.skip("Lua 5.4 unavailable")
    runner = CommandRunner()
    probe = runner.run(
        [lua, "-e",
         "require('busted.runner'); require('luacov.reporter.lcov'); require('dkjson')"],
        tmp_path, 15,
    )
    if probe.code != 0:
        pytest.skip(f"Busted/LuaCov toolchain unavailable: {probe.output}")

    source = tmp_path / "calc.lua"
    original = (
        "local M = {}\n"
        "function M.first(x)\n"
        "  return x + 1\n"
        "end\n"
        "return M\n"
    )
    source.write_bytes(original.encode("utf-8"))
    (tmp_path / ".busted").write_text(
        "return {default = {lpath = './?.lua;' .. package.path, helper = 'helper.lua'}}\n",
        encoding="utf-8",
    )
    (tmp_path / "helper.lua").write_text(
        "package.loaded['shared'] = {}\n", encoding="utf-8",
    )
    specs = tmp_path / "spec"
    specs.mkdir()
    (specs / "a_spec.lua").write_text(
        "local calc = require('calc')\n"
        "local shared = require('shared')\n"
        "it('prepares a cached result', function() shared.value = calc.first(10) end)\n",
        encoding="utf-8",
    )
    (specs / "b_spec.lua").write_text(
        "local shared = require('shared')\n"
        "it('checks the cached result', function() assert.equal(11, shared.value) end)\n",
        encoding="utf-8",
    )
    index = collect(tmp_path, source)
    assert index.select(source, 3) == ["spec/a_spec.lua"]
    options = dict(
        runner=runner, covered_lines={3}, ignore_coverage=False,
        mutate_all=True, lines=None, test_command=None, timeout_factor=10,
        mutation_warning=100, max_workers=1,
    )
    full = mutate_file(source, tmp_path, baselines={}, **options)
    selected = mutate_file(source, tmp_path, baselines={}, lua_coverage=index, **options)

    assert not full.baseline_failed, full.baseline_message
    assert not selected.baseline_failed, selected.baseline_message
    assert full.statuses and set(full.statuses.values()) == {"killed"}
    assert selected.statuses == full.statuses
    assert selected.forms == full.forms
    assert source.read_bytes() == original.encode("utf-8")


def test_opted_in_selection_can_kill_full_suite_survivor_with_shared_state(tmp_path):
    runners = ensure_crapper().runners
    lua = runners.lua_interpreter()
    if lua is None:
        pytest.skip("Lua 5.4 unavailable")
    runner = CommandRunner()
    probe = runner.run(
        [lua, "-e",
         "require('busted.runner'); require('luacov.reporter.lcov'); require('dkjson')"],
        tmp_path, 15,
    )
    if probe.code != 0:
        pytest.skip(f"Busted/LuaCov toolchain unavailable: {probe.output}")

    source = tmp_path / "calc.lua"
    original = (
        "local shared = require('shared')\n"
        "local M = {}\n"
        "function M.first(x)\n"
        "  return x + shared.offset\n"
        "end\n"
        "return M\n"
    )
    source.write_bytes(original.encode("utf-8"))
    (tmp_path / ".busted").write_text(
        "return {default = {lpath = './?.lua;' .. package.path, helper = 'helper.lua'}}\n",
        encoding="utf-8",
    )
    (tmp_path / "helper.lua").write_text(
        "package.loaded['shared'] = {offset = 1}\n", encoding="utf-8",
    )
    specs = tmp_path / "spec"
    specs.mkdir()
    (specs / "a_spec.lua").write_text(
        "local shared = require('shared')\n"
        "it('prepares shared state', function() shared.offset = 0 end)\n",
        encoding="utf-8",
    )
    (specs / "b_spec.lua").write_text(
        "local shared = require('shared')\n"
        "local calc = require('calc')\n"
        "it('checks the calculation', function()\n"
        "  assert.equal(10 + shared.offset, calc.first(10))\n"
        "end)\n",
        encoding="utf-8",
    )
    index = collect(tmp_path, source)
    assert index.select(source, 4) == ["spec/b_spec.lua"]
    options = dict(
        runner=runner, covered_lines={4}, ignore_coverage=False,
        mutate_all=True, lines=None, test_command=None, timeout_factor=10,
        mutation_warning=100, max_workers=1,
    )
    full = mutate_file(source, tmp_path, baselines={}, **options)
    # Opting into selection with shared-state specs violates its independence
    # requirement even when every pristine subset check passes.
    selected = mutate_file(source, tmp_path, baselines={}, lua_coverage=index,
                           select_specs=True, **options)

    assert not full.baseline_failed, full.baseline_message
    assert not selected.baseline_failed, selected.baseline_message
    assert [(site.original, site.mutant) for site in full.sites] == [("+", "-")]
    assert set(full.statuses.values()) == {"survived"}
    assert set(selected.statuses.values()) == {"killed"}
    assert selected.statuses.keys() == full.statuses.keys()
    assert source.read_bytes() == original.encode("utf-8")
