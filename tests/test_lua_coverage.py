import json
import os
import shutil
from pathlib import Path

import pytest

from mutator.crapper_link import ensure_crapper
from mutator.lua_coverage import LuaCoverageIndex, _decode, _literal, collect, load
from mutator.runner import CommandRunner


FIXTURE = Path(__file__).parent / "fixtures" / "lua_project"


@pytest.fixture
def project(tmp_path):
    directory = tmp_path / "project with spaces"
    (directory / "src").mkdir(parents=True)
    (directory / "spec").mkdir()
    (directory / ".busted").write_text(
        "return { default = {lpath = 'src/?.lua;src/?/init.lua'} }\n", encoding="utf-8"
    )
    source = directory / "src" / "calc.lua"
    source.write_text(
        "local M = {}\n"
        "function M.first()\n"
        "  return 11\n"
        "end\n"
        "function M.second()\n"
        "  return 22\n"
        "end\n"
        "return M\n", encoding="utf-8"
    )
    for name, value in (("first", 11), ("second", 22)):
        (directory / "spec" / f"{name}_spec.lua").write_text(
            "local calc = require('calc')\n"
            f"it('{name}', function() assert.equal({value}, calc.{name}()) end)\n",
            encoding="utf-8",
        )
    return directory, source


@pytest.fixture(params=["pure", "cluacov"])
def toolchain(request, monkeypatch):
    runners = ensure_crapper().runners
    lua = runners.lua_interpreter()
    if lua is None:
        pytest.skip("Lua 5.4 unavailable")
    setup = runners._lua_package_setup(lua)
    if request.param == "pure":
        setup += "package.preload['cluacov.version'] = function() error('pure LuaCov test') end; "
    else:
        # A local rock tree can provide the optional native hook without
        # changing or executing any project that owns that tree.
        checkout = Path(__file__).resolve().parents[1]
        git_pointer = checkout / '.git'
        if git_pointer.is_file():
            git_directory = Path(git_pointer.read_text(encoding='utf-8').strip().removeprefix('gitdir: '))
            if not git_directory.is_absolute():
                git_directory = checkout / git_directory
            checkout = git_directory.resolve().parents[2]
        tree = checkout.parent / "monopoly" / ".toolcache" / "luarocks"
        if tree.is_dir():
            setup += f"package.path = {json.dumps(tree.as_posix() + '/share/lua/5.4/?.lua;')} .. package.path; "
            extension = "dll" if os.name == "nt" else "so"
            setup += f"package.cpath = {json.dumps(tree.as_posix() + '/lib/lua/5.4/?.' + extension + ';')} .. package.cpath; "
    probe = CommandRunner().run(
        [lua, "-e", setup + "require('busted.runner'); require('luacov.reporter.lcov'); "
         + ("require('cluacov.hook'); " if request.param == "cluacov" else "") + "print('ready')"],
        Path(__file__).parent, 15,
    )
    if probe.code != 0:
        pytest.skip(f"Busted/LuaCov toolchain unavailable: {probe.output}")
    monkeypatch.setattr(runners, "_lua_package_setup", lambda _: setup)
    return runners


def test_real_suite_attributes_loads_and_independent_functions(project, toolchain):
    root, source = project
    index = collect(root, source)
    assert index.select(source, 3) == ["spec/first_spec.lua"]
    assert index.select(source, 6) == ["spec/second_spec.lua"]
    # Both require calls execute the module under default auto-insulation.
    assert index.select(source, 1) == ["spec/first_spec.lua", "spec/second_spec.lua"]
    assert index.select(source, 1000) is None
    assert index.select(root / "other" / "calc.lua", 3) is None
    assert set(index.specs) == {"spec/first_spec.lua", "spec/second_spec.lua"}
    assert index.seconds_for(list(index.specs)) == index.overhead + sum(index.specs.values())
    assert index.overhead > 0
    assert load(root, source) == index
    report = toolchain._coverage_report(root, root, "lua")
    coverage = toolchain._fresh_lcov(report, root)
    source_lines = next(v for k, v in coverage.items() if Path(k).resolve() == source.resolve())
    assert source_lines[3][0] > 0 and source_lines[6][0] > 0
    assert "-c" not in index.command
    assert not any("spec-coverage.pending" in arg for arg in index.command)


def test_selected_unrequired_source_and_scope_cache_identity(project, toolchain):
    root, source = project
    # Native Lua uses narrow paths; GitHub's Windows runner cannot open the
    # non-ASCII variant. The delimiter/quote escaping under test stays ASCII.
    suffix = "unused ' ]==].lua" if os.name == "nt" else "unused ' ü]==].lua"
    unused = root / "src" / suffix
    unused.write_text("local M = {}\nfunction M.zero()\n return 99\nend\nreturn M\n", encoding="utf-8")
    broken = root / ".claude" / "worktrees" / "broken"
    broken.mkdir(parents=True)
    (broken / "bad.lua").write_text("not valid Lua !!!", encoding="utf-8")
    collect(root, source)
    assert load(root, source, source_files=[source, unused]) is None
    index = collect(root, source, source_files=[source, unused, source], reuse=True)
    assert load(root, source, source_files=[unused, source]) == index
    assert load(root, source) is None
    fresh = toolchain._fresh_lcov(toolchain._coverage_report(root, root, "lua"), root)
    zero = next(lines for name, lines in fresh.items() if Path(name).resolve() == unused.resolve())
    assert zero and all(hits == 0 for hits, _ in zero.values())
    assert not any("broken" in name for name in fresh)


def test_spec_duration_includes_elapsed_sleep(project, toolchain):
    root, source = project
    spec = root / "spec" / "first_spec.lua"
    spec.write_text("require('system').sleep(0.05)\n" + spec.read_text(encoding="utf-8"), encoding="utf-8")
    index = collect(root, source)
    assert index.specs["spec/first_spec.lua"] >= 0.04


def test_helper_load_and_suite_setup_force_full_suite(project, toolchain):
    root, source = project
    (root / "helper.lua").write_text(
        "local calc = require('calc')\n"
        "require('busted').setup(function() calc.first() end)\n", encoding="utf-8"
    )
    (root / ".busted").write_text(
        "return { default = {lpath = 'src/?.lua', helper = 'helper.lua'} }\n", encoding="utf-8"
    )
    index = collect(root, source)
    assert index.select(source, 1) is None
    assert index.select(source, 3) is None
    assert index.select(source, 6) == ["spec/second_spec.lua"]


def test_exit_subscriber_coverage_is_outside_spec_context(project, toolchain):
    root, source = project
    (root / "helper.lua").write_text(
        "local calc = require('calc')\n"
        "require('busted').subscribe({'exit'}, function() calc.first(); return nil, true end)\n",
        encoding="utf-8",
    )
    (root / ".busted").write_text(
        "return { default = {lpath = 'src/?.lua', helper = 'helper.lua'} }\n", encoding="utf-8"
    )
    index = collect(root, source)
    assert index.select(source, 3) is None
    assert index.select(source, 6) == ["spec/second_spec.lua"]


def test_named_task_is_part_of_plan_and_cache_identity(project, toolchain):
    root, source = project
    (root / ".busted").write_text(
        "return { default = {lpath = 'src/?.lua'}, "
        "first = {ROOT = {'spec/first_spec.lua'}} }\n", encoding="utf-8"
    )
    index = collect(root, source, busted_task="first")
    assert index.command[-2:] == ["--run", "first"]
    assert set(index.specs) == {"spec/first_spec.lua"}
    assert load(root, source) is None
    assert load(root, source, busted_task="first") == index


def test_failed_suite_invalidates_existing_cache_and_lcov(project, toolchain):
    root, source = project
    collect(root, source)
    (root / "spec" / "first_spec.lua").write_text("it('fails', function() error('bad baseline') end)\n", encoding="utf-8")
    with pytest.raises(toolchain.CoverageError, match="Lua tests exited 1"):
        collect(root, source)
    report = toolchain._coverage_report(root, root, "lua")
    assert not report.exists()
    assert not (report.parent / "spec-coverage.json").exists()
    assert not (report.parent / "spec-coverage.pending.json").exists()
    assert load(root, source) is None


def test_corrupt_or_unsafe_cache_recollects_and_explicit_reuse_keeps_stale_source(project, toolchain):
    root, source = project
    first = collect(root, source)
    report = toolchain._coverage_report(root, root, "lua")
    cache = report.parent / "spec-coverage.json"
    source.write_text(source.read_text(encoding="utf-8") + "-- changed\n", encoding="utf-8")
    assert collect(root, source, reuse=True) == first
    for corrupt in ("{", '{"version": 1, "complete": false}'):
        cache.write_text(corrupt, encoding="utf-8")
        assert collect(root, source, reuse=True).select(source, 3) == ["spec/first_spec.lua"]
    raw = json.loads(cache.read_text(encoding="utf-8"))
    raw["specs"]["../outside_spec.lua"] = 0.1
    cache.write_text(json.dumps(raw), encoding="utf-8")
    assert load(root, source) is None
    assert collect(root, source, reuse=True).select(source, 6) == ["spec/second_spec.lua"]


def test_reuse_rejects_mismatched_artifacts_and_invalid_overhead(project, toolchain):
    root, source = project
    index = collect(root, source)
    assert load(root, source).overhead == index.overhead
    report = toolchain._coverage_report(root, root, "lua")
    stats = report.parent / "luacov.stats.out"
    cache = report.parent / "spec-coverage.json"
    for artifact in (report, stats):
        original = artifact.read_bytes()
        artifact.write_bytes(original + b"\n")
        assert load(root, source) is None
        artifact.write_bytes(original)
        assert load(root, source) == index
    raw = json.loads(cache.read_text(encoding="utf-8"))
    raw["overhead"] = -1
    cache.write_text(json.dumps(raw), encoding="utf-8")
    assert load(root, source) is None
    raw["overhead"] = True
    cache.write_text(json.dumps(raw), encoding="utf-8")
    assert load(root, source) is None


def test_compact_publication_uses_ids_and_rejects_old_or_invalid_ids(project, toolchain):
    root, source = project
    collect(root, source)
    cache = toolchain._coverage_report(root, root, "lua").parent / "spec-coverage.json"
    original = cache.read_text(encoding="utf-8")
    raw = json.loads(original)
    assert raw["version"] == 2
    assert raw["specIds"] == sorted(raw["specs"])
    memberships = [members for covered in raw["lines"].values() for members in covered.values()]
    assert memberships and all(type(member) is int for members in memberships for member in members)
    source_lines = next(covered for name, covered in raw["lines"].items() if Path(name).resolve() == source.resolve())
    for bad in ([True], [-1], [len(raw["specIds"])], [0, 0], ["spec/first_spec.lua"]):
        source_lines["3"] = bad
        cache.write_text(json.dumps(raw), encoding="utf-8")
        assert load(root, source) is None
    raw = json.loads(original)
    raw["version"] = 1
    cache.write_text(json.dumps(raw), encoding="utf-8")
    assert load(root, source) is None


def test_preloaded_function_and_coroutine_survive_segment_resets(project, toolchain):
    root, source = project
    (root / "helper.lua").write_text(
        "local calc = require('calc')\n"
        "shared_calc = calc\n"
        "shared_coroutine = coroutine.create(function()\n"
        "  calc.first(); coroutine.yield(); calc.first()\n"
        "end)\n", encoding="utf-8",
    )
    (root / ".busted").write_text(
        "return { default = {lpath = 'src/?.lua', helper = 'helper.lua', ['sort-files'] = true} }\n",
        encoding="utf-8",
    )
    for name in ("first", "second"):
        (root / "spec" / f"{name}_spec.lua").write_text(
            "it('shared', function()\n"
            "  assert(coroutine.resume(shared_coroutine))\n"
            "  require('luacov.runner').save_stats()\n"
            "  assert.equal(11, shared_calc.first())\n"
            "end)\n", encoding="utf-8",
        )
    index = collect(root, source)
    assert index.select(source, 3) == ["spec/first_spec.lua", "spec/second_spec.lua"]
    stats = toolchain._coverage_report(root, root, "lua").parent / "luacov.stats.out"
    rows = stats.read_text(encoding="utf-8").splitlines()
    counts = next(rows[n + 1].split() for n, row in enumerate(rows) if row.endswith("calc.lua"))
    assert int(counts[2]) == 4


def test_tick_saves_preserve_segments_and_hook(project, toolchain):
    root, source = project
    (root / "helper.lua").write_text(
        "local runner = require('luacov.runner')\n"
        "runner.tick = true\n"
        "runner.configuration.savestepsize = 10000\n", encoding="utf-8",
    )
    (root / ".busted").write_text(
        "return { default = {lpath = 'src/?.lua', helper = 'helper.lua'} }\n", encoding="utf-8",
    )
    (root / "spec" / "first_spec.lua").write_text(
        "local calc = require('calc')\n"
        "it('tick saves', function()\n"
        "  for _ = 1, 12000 do assert.equal(11, calc.first()) end\n"
        "end)\n", encoding="utf-8",
    )
    index = collect(root, source)
    assert index.select(source, 3) == ["spec/first_spec.lua"]
    assert index.select(source, 6) == ["spec/second_spec.lua"]
    stats = toolchain._coverage_report(root, root, "lua").parent / "luacov.stats.out"
    rows = stats.read_text(encoding="utf-8").splitlines()
    counts = next(rows[n + 1].split() for n, row in enumerate(rows) if row.endswith("calc.lua"))
    assert int(counts[2]) == 12000


def test_nested_coroutines_and_explicit_stats_save_retain_aggregate(project, toolchain):
    root, source = project
    (root / "spec" / "first_spec.lua").write_text(
        "local calc = require('calc')\n"
        "it('nested coroutines', function()\n"
        "  coroutine.wrap(function()\n"
        "    coroutine.wrap(function() assert.equal(11, calc.first()) end)()\n"
        "  end)()\n"
        "  require('luacov.runner').save_stats()\n"
        "  assert.equal(11, calc.first())\n"
        "end)\n", encoding="utf-8"
    )
    index = collect(root, source)
    assert index.select(source, 3) == ["spec/first_spec.lua"]
    assert index.select(source, 6) == ["spec/second_spec.lua"]
    fresh = toolchain._fresh_lcov(toolchain._coverage_report(root, root, "lua"), root)
    lines = next(v for k, v in fresh.items() if Path(k).resolve() == source.resolve())
    assert lines[3][0] > 0
    assert lines[6][0] > 0
    stats = toolchain._coverage_report(root, root, "lua").parent / "luacov.stats.out"
    rows = stats.read_text(encoding="utf-8").splitlines()
    counts = next(rows[n + 1].split() for n, row in enumerate(rows) if row.endswith("calc.lua"))
    assert int(counts[2]) >= 2


def test_existing_real_fixture_keeps_coverage(tmp_path, toolchain):
    root = tmp_path / "fixture"
    shutil.copytree(FIXTURE, root)
    source = root / "src" / "calc" / "init.lua"
    index = collect(root, source)
    assert index.select(source, 8) == ["spec/calc_spec.lua"]
    fresh = toolchain._fresh_lcov(toolchain._coverage_report(root, root, "lua"), root)
    assert any(Path(name).name == "util.lua" for name in fresh)


def test_index_is_exact_and_fallback_dominates(tmp_path):
    source = tmp_path / "src" / "calc.lua"
    key = os.path.normcase(str(source.resolve()))
    index = LuaCoverageIndex(tmp_path, [], {"spec/a.lua": 0.2}, {key: {3: {"spec/a.lua"}, 4: {"spec/a.lua"}}}, {key: {4}})
    assert index.select(source, 3) == ["spec/a.lua"]
    assert index.select(source, 4) is None
    assert index.select(tmp_path / "calc.lua", 3) is None
    assert index.select(source, 99) is None
    assert index.seconds_for(["spec/a.lua", "spec/a.lua"]) == 0.2
    index.overhead = 3.0
    assert index.seconds_for(["spec/a.lua"]) == 3.2


def test_unproven_attribution_forces_fallback(project):
    root, source = project
    raw = {
        "version": 2, "complete": True, "unsafe": True,
        "specs": {"spec/first_spec.lua": 0.1},
        "specIds": ["spec/first_spec.lua"],
        "lines": {"src/calc.lua": {"3": [0]}},
        "fallback": {},
    }
    index = _decode(raw, root, [], published=False)
    assert index.select(source, 3) is None
    raw["unsafe"] = False
    raw["lines"]["src/calc.lua"]["3"] = [1]
    with pytest.raises(ValueError, match="Invalid spec membership"):
        _decode(raw, root, [], published=False)


def _profile_collector(toolchain, tmp_path, collector=None, line_count=128, spec_count=80):
    """Isolate retained memberships and publication from Busted/project memory."""
    collector = collector or Path(__file__).parents[1] / "src" / "mutator" / "lua_coverage.lua"
    destination = tmp_path / "synthetic.json"
    script = tmp_path / "profile.lua"
    script.write_text("""
local collector, destination, line_count, spec_count = ...
line_count, spec_count = tonumber(line_count), tonumber(spec_count)
local callbacks = {}
local runner = {data = {}, initialized = true, configuration = {statsfile = 'unused'}}
runner.file_included = function() return true end
runner.shutdown = function() runner.save_stats() end
package.loaded['luacov.runner'] = runner
package.loaded['luacov.stats'] = {save = function() end}
package.loaded['busted.core'] = function()
  return {
    monotime = os.clock,
    subscribe = function(event, callback) callbacks[event[2]] = callback end,
  }
end
local finish = assert(loadfile(collector))()(destination)
require('busted.core')()
local peak = collectgarbage('count')
local function sample()
  peak = math.max(peak, collectgarbage('count'))
end
local function counters(id)
  local file = {}
  for line = 1, line_count do
    if line % 3 == 0 or id % 17 == line % 17 then file[line] = 1 end
  end
  runner.data['src/synthetic.lua'] = file
end
-- Outside-spec and unknown lines must remain distinct in the publication.
runner.data['src/synthetic.lua'] = {[line_count + 1] = 1, [3] = 1}
for id = 0, spec_count - 1 do
  local file = {name = 'spec/' .. id .. '.lua'}
  callbacks.start(file)
  counters(id)
  runner.save_stats()
  counters(id)
  callbacks['end'](file)
  sample()
end
-- A repeated spec keeps its original ID and does not duplicate memberships.
callbacks.start({name = 'spec/0.lua'})
counters(0)
callbacks['end']({name = 'spec/0.lua'})
sample()
local retained = collectgarbage('count')
local open = io.open
io.open = function(...)
  local file = assert(open(...))
  return {
    write = function(_, value) sample(); return file:write(value) end,
    close = function() sample(); return file:close() end,
  }
end
finish()
sample()
print(string.format('retained_kib=%.1f peak_kib=%.1f', retained, peak))
""", encoding="utf-8")
    lua = toolchain.lua_interpreter()
    setup = toolchain._lua_package_setup(lua)
    result = CommandRunner().run(
        [lua, "-e", setup + "assert(loadfile(arg[1]))(table.unpack(arg, 2))",
         "--", str(script), str(collector), str(destination), str(line_count), str(spec_count)],
        tmp_path, 30,
    )
    assert result.code == 0, result.output
    return json.loads(destination.read_text(encoding="utf-8")), result.output


def test_packed_memberships_cross_byte_and_word_boundaries(tmp_path, toolchain):
    raw, _memory = _profile_collector(toolchain, tmp_path)
    assert raw["specIds"] == [f"spec/{id}.lua" for id in range(80)]
    assert raw["unsafe"] is False
    assert raw["fallback"] == {"src/synthetic.lua": {"129": True, "3": True}}
    lines = raw["lines"]["src/synthetic.lua"]
    assert set(lines) == {str(line) for line in range(1, 129)}
    for line in range(1, 129):
        assert lines[str(line)] == [id for id in range(80) if line % 3 == 0 or id % 17 == line % 17]
    (tmp_path / "spec").mkdir()
    for name in raw["specIds"]:
        (tmp_path / name).write_text("", encoding="utf-8")
    index = _decode(raw, tmp_path, [], published=False)
    source = tmp_path / "src" / "synthetic.lua"
    assert index.select(source, 3) is None
    assert index.select(source, 129) is None
    assert index.select(source, 130) is None
    assert index.fallback == {os.path.normcase(str(source.resolve())): {3, 129}}


def test_streamed_publication_escapes_source_names(project, toolchain):
    root, source = project
    # See the narrow-path note above; quotes and brackets still exercise escaping.
    name = 'calc [quoted].lua' if os.name == 'nt' else 'calc [quoted] ü.lua'
    unusual = source.with_name(name)
    source.rename(unusual)
    for spec in (root / 'spec').glob('*.lua'):
        spec.write_text(spec.read_text(encoding='utf-8').replace("require('calc')", f"require('{unusual.stem}')"), encoding='utf-8')
    index = collect(root, unusual)
    assert index.select(unusual, 3) == ['spec/first_spec.lua']
    assert index.select(unusual, 6) == ['spec/second_spec.lua']


def test_lua_literal_handles_windows_paths_and_delimiters(project, toolchain):
    root, _source = project
    value = "C:\\project space\\some]=]file]==]name.lua"
    lua = toolchain.lua_interpreter()
    result = CommandRunner().run([lua, "-e", "io.write(" + _literal(value) + ")"], root, 5)
    assert result.code == 0
    assert result.output == value
