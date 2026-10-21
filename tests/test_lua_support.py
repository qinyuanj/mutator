"""lua_support.lua against the real Lua 5.4 + Busted in the LuaRocks tree."""
import os
from pathlib import Path

import pytest

from mutator.crapper_link import ensure_crapper
from mutator.runner import CommandRunner
from mutator.runner import test_plan as command_for


@pytest.fixture
def lua(runners=None):
    interpreter = ensure_crapper().runners.lua_interpreter()
    if interpreter is None:
        pytest.skip("Lua 5.4 unavailable")
    if not any(
        (Path(tree) / "share/lua/5.4/busted/runner.lua").is_file()
        for tree in _trees()
    ):
        pytest.skip("Busted unavailable in the LuaRocks trees")
    return interpreter


def _trees():
    from mutator.runner import _rocks_trees

    return _rocks_trees()


def busted_project(root, spec_text):
    source = root / "src" / "calc.lua"
    source.parent.mkdir(parents=True)
    source.write_text("return { add = function(x) return x + 1 end }\n", encoding="utf-8")
    (root / ".busted").write_text(
        'return { default = { ROOT = { "spec" }, pattern = "_spec" } }\n', encoding="utf-8")
    spec = root / "spec" / "probe_spec.lua"
    spec.parent.mkdir()
    spec.write_text(spec_text, encoding="utf-8")
    return source


def run(command, cwd, **env):
    previous = {name: os.environ.get(name) for name in env}
    os.environ.update(env)
    try:
        return CommandRunner().run(command, cwd, 60)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_pinned_paths_exclude_distribution_templates(tmp_path, lua):
    source = busted_project(tmp_path, """
describe('pinning', function()
  it('confines absolute templates to the rocks trees', function()
    for entry in package.path:gmatch('[^;]+') do
      assert(not entry:match('^/usr/s?hare/'), 'distribution template kept: ' .. entry)
    end
    assert(package.path:find('?.lua', 1, true), 'module templates missing')
  end)
end)
""")
    command, cwd = command_for(tmp_path, source, "lua", None)
    result = run(command, cwd)
    assert result.code == 0, result.output


def test_compile_cache_reuses_bytecode_and_respects_content(tmp_path, lua):
    source = busted_project(tmp_path, """
describe('compile cache', function()
  it('serves repeated loads from bytecode', function()
    local first = load('return 40 + 2', '@probe.lua')
    assert(MUTATOR_LUA_CACHE.misses > 0)
    local before = MUTATOR_LUA_CACHE.hits
    local second = load('return 40 + 2', '@probe.lua')
    assert(MUTATOR_LUA_CACHE.hits == before + 1, 'cache hit missing')
    assert(first() == second())
  end)
  it('keys chunks by content, not only by name', function()
    assert(load('return 1', '@same.lua')() == 1)
    assert(load('return 2', '@same.lua')() == 2)
  end)
  it('loadfile keeps chunk names and line numbers', function()
    local path = 'spec/chunk.lua'
    local file = io.open(path, 'w')
    file:write('local x = 1\\nerror("boom")\\n')
    file:close()
    local chunk, err = loadfile(path)
    local ok, message = pcall(chunk)
    assert(not ok and message:match('chunk.lua:2: boom'), tostring(message))
  end)
end)
""")
    command, cwd = command_for(tmp_path, source, "lua", None)
    result = run(command, cwd)
    assert result.code == 0, result.output


FAILING_SPEC = """
describe('fasttrace', function()
  it('fails with a traceback', function()
    local function boom() error('expected failure', 2) end
    boom()
  end)
end)
"""


def test_fasttrace_failure_output_matches_stock(tmp_path, lua):
    command, cwd = command_for(tmp_path, busted_project(tmp_path, FAILING_SPEC), "lua", None)
    patched = run([*command, "--verbose"], cwd)
    stock = run([*command, "--verbose"], cwd, MUTATOR_LUA_NO_FASTTRACE="1")
    assert patched.code != 0 and stock.code != 0
    assert "stack traceback:" in patched.output

    def normalized(output):
        return "\n".join(
            line for line in output.splitlines() if not line.endswith(" seconds")
        )

    assert normalized(patched.output) == normalized(stock.output)


def test_fasttrace_reports_the_failing_frame(tmp_path, lua):
    command, cwd = command_for(tmp_path, busted_project(tmp_path, FAILING_SPEC), "lua", None)
    result = run([*command, "--verbose"], cwd)
    assert "probe_spec.lua:4" in result.output
