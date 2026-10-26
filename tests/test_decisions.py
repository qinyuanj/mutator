"""Each run reports what it decided for every site, apart from the snapshot totals."""

import sys

from mutator.cli import run
from mutator.engine import _decide, mutate_file
from mutator.model import Decision, History, PriorForm, RunResult, Site
from mutator.report import format_decisions
from mutator.workers import WorkerPool

from test_engine import SOURCE, FakeRunner
from test_lua_queue import EngineRunner, engine_fixture, mutate


def _mutate(path, root, **overrides):
    options = dict(
        runner=FakeRunner(), covered_lines=None, ignore_coverage=True, mutate_all=False,
        lines=None, test_command="fake", timeout_factor=10, mutation_warning=50, baselines={},
    )
    options.update(overrides)
    return mutate_file(path, root, **options)


def _demo(tmp_path, source=SOURCE):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_a_first_run_executes_every_site_for_lack_of_history(tmp_path):
    result = _mutate(_demo(tmp_path), tmp_path)
    assert {result.decisions[site.mutation_id] for site in result.sites} == {
        Decision("executed", "no-history", "none")}


def test_an_unchanged_rerun_reuses_kills_and_reruns_survivors(tmp_path):
    path = _demo(tmp_path)
    first = _mutate(path, tmp_path)
    second = _mutate(path, tmp_path)
    for site in second.sites:
        previous = first.statuses[site.mutation_id]
        expected = (Decision("reused", "unchanged-killed", "killed") if previous == "killed"
                    else Decision("executed", "previous-survived", "survived"))
        assert second.decisions[site.mutation_id] == expected


def test_mutate_all_overrides_history(tmp_path):
    path = _demo(tmp_path)
    _mutate(path, tmp_path)
    again = _mutate(path, tmp_path, mutate_all=True)
    assert {decision.reason for decision in again.decisions.values()} == {"explicit-all"}
    assert {decision.action for decision in again.decisions.values()} == {"executed"}


def test_an_edited_function_reruns_because_its_digest_changed(tmp_path):
    path = _demo(tmp_path)
    _mutate(path, tmp_path)
    _demo(tmp_path, SOURCE.replace("return a + b\n", "return b + a\n", 1))
    edited = _mutate(path, tmp_path)
    assert {decision.reason for decision in edited.decisions.values()} == {"digest-changed"}


def test_uncovered_and_line_filtered_sites_are_not_reuse(tmp_path):
    path = _demo(tmp_path)
    result = _mutate(path, tmp_path, ignore_coverage=False, covered_lines={2, 3, 4}, lines={2})
    assert result.decisions == {
        site.mutation_id: (Decision("executed", "no-history", "none") if site.line == 2
                           else Decision("excluded", "line-filter", "none"))
        for site in result.sites
    }
    uncovered = _mutate(path, tmp_path, ignore_coverage=False, covered_lines={3, 4})
    assert {uncovered.decisions[site.mutation_id] for site in uncovered.sites if site.line == 2} == {
        Decision("uncovered", "uncovered", "survived")}


def _site(line, mutation_id):
    return Site("src/demo.py", "demo", "defn/add", "add", line, 0, 1, "+", "-", "arith", mutation_id)


def test_an_unchanged_function_reruns_a_site_whose_id_it_has_not_seen():
    sites = [_site(2, "known"), _site(3, "new")]
    history = History({("demo", "defn/add"): PriorForm("demo", "defn/add", "d", "src/demo.py")},
                      {"known": "killed"})
    decisions = _decide(sites, {"known": True, "new": True}, history,
                        {("demo", "defn/add"): "d"}, mutate_all=False, lines=None)
    assert decisions == {"known": Decision("reused", "unchanged-killed", "killed"),
                         "new": Decision("executed", "id-changed", "none")}


def test_the_shared_lua_queue_reports_decisions_too(tmp_path, monkeypatch):
    engine, sources, _published = engine_fixture(tmp_path, monkeypatch)
    with WorkerPool(tmp_path, sources, check_each_worker=False) as pool:
        results = mutate(engine, tmp_path, sources, EngineRunner(), pool)
    for result in results:
        assert len(result.decisions) == 2
        assert {d.reason for d in result.decisions.values()} == {"explicit-all"}


def _result():
    sites = [_site(2, "a"), _site(3, "b"), _site(4, "c"),
             Site("src/demo.py", "demo", "defn/sub", "sub", 9, 0, 1, "-", "+", "arith", "d")]
    return RunResult(
        path="src/demo.py", forms=[], written=[], sites=sites,
        statuses={"a": "killed", "b": "survived", "c": "uncovered", "d": "killed"},
        decisions={"a": Decision("reused", "unchanged-killed", "killed"),
                   "b": Decision("executed", "previous-survived", "survived"),
                   "c": Decision("uncovered", "uncovered", "none"),
                   "d": Decision("executed", "no-history", "none")},
    )


def test_decisions_are_counted_per_function_and_per_run():
    assert format_decisions([_result()]) == (
        "Decisions demo/add: executed=1 reused=1 uncovered=1 excluded=0\n"
        "Decisions demo/sub: executed=1 reused=0 uncovered=0 excluded=0\n"
        "Decisions total: executed=2 reused=1 uncovered=1 excluded=0 "
        "(no-history=1 previous-survived=1 unchanged-killed=1 uncovered=1)\n"
    )


def test_verbose_decisions_add_one_line_per_site():
    text = format_decisions([_result()], verbose=True)
    assert ("src/demo.py:3 demo/add + -> - executed reason=previous-survived "
            "previous=survived result=survived id=b") in text.splitlines()
    assert sum(" reason=" in line for line in text.splitlines()) == 4


def test_no_decisions_print_nothing():
    assert format_decisions([]) == ""


def test_the_cli_prints_the_run_decisions(tmp_path, capsys):
    _demo(tmp_path)
    command = (f"{sys.executable} -c \"import sys; sys.path.insert(0, 'src'); "
               "from demo import add; raise SystemExit(0 if add(1, 2) == 3 else 1)\"")
    args = ["--no-coverage", "--root", str(tmp_path), "--test-command", command, "src/demo.py"]
    run(args)
    out = capsys.readouterr().out
    assert "Decisions total: executed=4 reused=0 uncovered=0 excluded=0 (no-history=4)" in out
    assert " reason=" not in out
    run([*args, "--verbose"])
    out = capsys.readouterr().out
    assert "Decisions total: executed=3 reused=1" in out
    assert "reused reason=unchanged-killed previous=killed result=killed" in out


def test_functions_sharing_a_name_keep_separate_counts():
    twin = Site("src/demo.py", "demo", "impl/Other/add", "add", 20, 0, 1, "+", "-", "arith", "e")
    result = _result()
    result.sites.append(twin)
    result.decisions["e"] = Decision("executed", "no-history", "none")
    lines = format_decisions([result]).splitlines()
    assert "Decisions demo/add: executed=1 reused=1 uncovered=1 excluded=0" in lines
    assert "Decisions demo/add: executed=1 reused=0 uncovered=0 excluded=0" in lines


def test_a_run_with_nothing_to_execute_still_reports_its_reuse(tmp_path, capsys):
    _demo(tmp_path)
    command = (f"{sys.executable} -c \"import sys; sys.path.insert(0, 'src'); "
               "from demo import add; raise SystemExit(0 if add(1, 2) == 3 else 1)\"")
    args = ["--no-coverage", "--root", str(tmp_path), "--test-command", command, "src/demo.py"]
    run([*args, "--lines", "3"])
    capsys.readouterr()
    run([*args, "--lines", "3"])
    out = capsys.readouterr().out
    assert "No mutations selected" in out
    assert "Decisions total: executed=0 reused=1 uncovered=0 excluded=3" in out
