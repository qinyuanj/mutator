import os
import sys
from pathlib import Path

import pytest

from mutator.engine import _covering_first
from mutator.model import Site
from mutator.runner import _memory_limit


class Index:
    specs = {"spec/a_spec.lua": 1.0, "spec/b_spec.lua": 1.0, "spec/c_spec.lua": 1.0, "spec/d_spec.lua": 1.0}

    def select(self, source, line):
        return {3: ["spec/d_spec.lua"], 5: ["spec/c_spec.lua", "spec/d_spec.lua"]}.get(line)


def site(line):
    return Site(file="src/x.lua", namespace="x", form_id="defn/f", name="f", line=line, start=0, end=1,
                original="+", mutant="-", category="arithmetic", mutation_id=str(line))


def test_covering_specs_run_first_and_every_spec_still_runs():
    order = _covering_first(Index(), Path("src/x.lua"), [site(3), site(5), site(9)])
    assert order == ["spec/c_spec.lua", "spec/d_spec.lua", "spec/a_spec.lua", "spec/b_spec.lua"]


def test_uncovered_file_keeps_suite_order():
    assert _covering_first(Index(), Path("src/x.lua"), [site(9)]) == sorted(Index.specs)


@pytest.mark.skipif(os.name != "posix", reason="RLIMIT_AS is POSIX")
def test_memory_cap_applies_only_to_lua(monkeypatch):
    monkeypatch.delenv("MUTATOR_LUA_MEMORY_MB", raising=False)
    assert _memory_limit(["env", "LUA_PATH=x", "/usr/bin/lua5.4", "-e", "x"]) is not None
    assert _memory_limit([sys.executable, "-m", "pytest"]) is None
    assert _memory_limit("lua5.4 -e x") is None
    monkeypatch.setenv("MUTATOR_LUA_MEMORY_MB", "0")
    assert _memory_limit(["lua5.4", "-e", "x"]) is None
