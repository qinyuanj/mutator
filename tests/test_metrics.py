import dataclasses
from pathlib import Path

import pytest

from mutator.edn import dumps, loads
from mutator.metrics import load_history, snapshot_path, write_results
from mutator.model import FormResult


def _form(namespace, name, file_name, digest, killed=1, private=False):
    form_id = f"defn-/{name}" if private else f"defn/{name}"
    return FormResult(
        id=form_id,
        namespace=namespace,
        name=name,
        private=private,
        file=file_name,
        line=2,
        end_line=4,
        digest=digest,
        killed=killed,
        survived=0,
        uncovered=1,
        sites=2,
    )


def _viewer_name(form_id):
    """The same split uml-viewer.application.overlay/form-name performs."""

    if form_id.startswith("defn-/"):
        return form_id[6:], True
    if form_id.startswith("defn/"):
        return form_id[5:], False
    return None, False


def test_one_snapshot_per_namespace_preserves_the_other_file(tmp_path):
    write_results(
        tmp_path,
        "a.go",
        [_form("demo", "Run", "a.go", "hash-a")],
        {},
    )
    write_results(
        tmp_path,
        "b.go",
        [_form("demo", "Stop", "b.go", "hash-b", killed=4)],
        {},
    )
    path = snapshot_path(tmp_path, "demo")
    data = loads(path.read_text(encoding="utf-8"))
    assert data["namespace"] == "demo"
    names = []
    for form in data["forms"]:
        operation, private = _viewer_name(form["id"])
        names.append((operation, private, form["killed"], form["uncovered"], form["sites"]))
    assert ("Run", False, 1, 1, 2) in names
    assert ("Stop", False, 4, 1, 2) in names
    assert list(tmp_path.joinpath(".metrics", "mutate").rglob("*.edn")) == [path]

    write_results(
        tmp_path,
        "a.go",
        [_form("demo", "Run", "a.go", "hash-a", killed=9)],
        {},
    )
    again = loads(path.read_text(encoding="utf-8"))
    by_id = {form["id"]: form for form in again["forms"]}
    assert by_id["defn/Run"]["killed"] == 9
    assert by_id["defn/Stop"]["killed"] == 4


def test_private_form_id_and_history_round_trip(tmp_path):
    outcome = '["hide.clj","demo.core","defn-/hide",8,12,"false","true"]'
    write_results(
        tmp_path,
        "hide.clj",
        [_form("demo.core", "hide", "hide.clj", "digest", private=True)],
        {outcome: "killed"},
    )
    data = loads(snapshot_path(tmp_path, "demo.core").read_text(encoding="utf-8"))
    assert _viewer_name(data["forms"][0]["id"]) == ("hide", True)
    history = load_history(tmp_path, "hide.clj")
    assert history.forms[("demo.core", "defn-/hide")].digest == "digest"
    assert history.outcomes[outcome] == "killed"


def test_snapshot_paths_follow_the_namespace(tmp_path):
    assert snapshot_path(tmp_path, "example.com/demo.Widget") == (
        tmp_path / ".metrics" / "mutate" / "example.com" / "demo.Widget.edn"
    )
    assert snapshot_path(tmp_path, "crate::foo::Bar") == (
        tmp_path / ".metrics" / "mutate" / "crate" / "foo" / "Bar.edn"
    )


def test_a_non_canonical_snapshot_is_folded_into_the_namespace_file(tmp_path):
    stale = tmp_path / ".metrics" / "mutate" / "legacy.edn"
    stale.parent.mkdir(parents=True)
    stale.write_text(
        dumps(
            {
                "version": 1,
                "namespace": "demo",
                "source": "b.go",
                "forms": [
                    {
                        "id": "defn/Stop",
                        "file": "b.go",
                        "line": 3,
                        "hash": "hash-b",
                        "killed": 4,
                        "survived": 0,
                        "uncovered": 1,
                        "sites": 2,
                    }
                ],
                "outcomes": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    write_results(tmp_path, "a.go", [_form("demo", "Run", "a.go", "hash-a")], {})
    assert not stale.exists()
    data = loads(snapshot_path(tmp_path, "demo").read_text(encoding="utf-8"))
    by_id = {form["id"]: form for form in data["forms"]}
    assert by_id["defn/Stop"]["killed"] == 4
    assert by_id["defn/Run"]["file"] == "a.go"


def test_replacement_failure_preserves_shared_snapshot_and_legacy_records(tmp_path, monkeypatch):
    write_results(tmp_path, "a.go", [_form("demo", "Run", "a.go", "hash-a")], {})
    path = snapshot_path(tmp_path, "demo")
    original = path.read_bytes()
    legacy = path.with_name("legacy.edn")
    legacy.write_bytes(original)

    def fail_replace(self, target):
        assert Path(target) == path
        # Readers still see the complete old artifact before the replacement.
        assert path.read_bytes() == original
        raise OSError("injected replacement failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replacement failure"):
        write_results(tmp_path, "b.go", [_form("demo", "Stop", "b.go", "hash-b")], {})
    assert path.read_bytes() == original
    assert legacy.read_bytes() == original
    assert load_history(tmp_path, "a.go").forms[("demo", "defn/Run")].digest == "hash-a"
    assert sorted(path.parent.iterdir()) == sorted([path, legacy])


def test_publishing_only_replaces_affected_namespaces_including_stale_records(tmp_path):
    write_results(tmp_path, "a.go", [_form("old", "Run", "a.go", "hash-a")], {})
    write_results(tmp_path, "b.go", [_form("old", "Stop", "b.go", "hash-b")], {})
    write_results(tmp_path, "other.go", [_form("unrelated", "Other", "other.go", "other")], {})
    unrelated = snapshot_path(tmp_path, "unrelated")
    unrelated.touch()
    before = (unrelated.read_bytes(), unrelated.stat().st_mtime_ns)
    written = write_results(tmp_path, "a.go", [_form("new", "Run", "a.go", "new-a")], {})
    assert set(written) == {snapshot_path(tmp_path, "old"), snapshot_path(tmp_path, "new")}
    assert (unrelated.read_bytes(), unrelated.stat().st_mtime_ns) == before
    old = loads(snapshot_path(tmp_path, "old").read_text(encoding="utf-8"))
    assert [form["file"] for form in old["forms"]] == ["b.go"]


def test_shared_legacy_forms_keep_their_original_source_ownership(tmp_path):
    path = snapshot_path(tmp_path, "demo")
    path.parent.mkdir(parents=True)
    path.write_text(dumps({
        "namespace": "demo", "source": "a.go",
        "forms": [{"id": "defn/Run", "line": 2, "hash": "hash-a", "killed": 1}],
        "outcomes": {},
    }), encoding="utf-8")
    write_results(tmp_path, "b.go", [_form("demo", "Stop", "b.go", "hash-b")], {})
    assert load_history(tmp_path, "a.go").forms[("demo", "defn/Run")].digest == "hash-a"
    write_results(tmp_path, "b.go", [], {})
    assert load_history(tmp_path, "a.go").forms[("demo", "defn/Run")].digest == "hash-a"


def test_failure_in_second_artifact_keeps_first_publication_and_shared_outcomes(tmp_path, monkeypatch):
    a_outcome = '["a.go","z","defn/Run",8,12,"false","true"]'
    b_outcome = '["b.go","z","defn/Stop",8,12,"false","true"]'
    write_results(tmp_path, "a.go", [_form("z", "Run", "a.go", "hash-a")], {a_outcome: "killed"})
    write_results(tmp_path, "b.go", [_form("z", "Stop", "b.go", "old-b")], {b_outcome: "survived"})
    z = snapshot_path(tmp_path, "z")
    original = z.read_bytes()
    legacy = z.with_name("legacy.edn")
    legacy.write_bytes(original)
    replace = Path.replace

    def fail_second(self, target):
        if Path(target) == z:
            raise OSError("second artifact failed")
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_second)
    with pytest.raises(OSError, match="second artifact failed"):
        write_results(tmp_path, "b.go", [
            _form("first", "Fresh", "b.go", "fresh-b"),
            _form("z", "Stop", "b.go", "new-b"),
        ], {b_outcome: "killed"})
    first = loads(snapshot_path(tmp_path, "first").read_text(encoding="utf-8"))
    assert first["forms"][0]["hash"] == "fresh-b"
    assert z.read_bytes() == original
    assert legacy.read_bytes() == original
    assert load_history(tmp_path, "a.go").outcomes == {a_outcome: "killed"}
    assert load_history(tmp_path, "b.go").outcomes == {b_outcome: "survived"}
    assert not list(z.parent.glob("*.tmp"))


def test_stale_cleanup_preserves_shared_outcomes_without_forms(tmp_path):
    a_outcome = '["a.go","demo","defn/Run",8,12,"false","true"]'
    b_outcome = '["b.go","demo","defn/Stop",8,12,"false","true"]'
    path = snapshot_path(tmp_path, "demo")
    path.parent.mkdir(parents=True)
    path.write_text(dumps({
        "namespace": "demo", "source": "b.go", "forms": [],
        "outcomes": {a_outcome: ":killed", b_outcome: ":survived"},
    }), encoding="utf-8")
    assert write_results(tmp_path, "b.go", [], {}) == [path]
    assert load_history(tmp_path, "a.go").outcomes == {a_outcome: "killed"}
    assert load_history(tmp_path, "b.go").outcomes == {}


def test_dropping_a_file_removes_its_namespace_when_nothing_else_remains(tmp_path):
    write_results(tmp_path, "a.go", [_form("demo", "Run", "a.go", "hash-a")], {})
    write_results(tmp_path, "a.go", [], {})
    assert not snapshot_path(tmp_path, "demo").exists()


def test_forms_are_ordered_by_file_then_line(tmp_path):
    late = dataclasses.replace(_form("demo", "alpha", "a.go", "h1"), line=9)
    early = dataclasses.replace(_form("demo", "zeta", "a.go", "h2"), line=1)
    write_results(tmp_path, "a.go", [late, early], {})
    legacy = loads(snapshot_path(tmp_path, "demo").read_text(encoding="utf-8"))
    legacy["forms"][0].pop("line")
    snapshot_path(tmp_path, "demo").write_text(dumps(legacy) + "\n", encoding="utf-8")
    write_results(tmp_path, "b.go", [dataclasses.replace(_form("demo", "beta", "b.go", "h3"), line=1)], {})
    data = loads(snapshot_path(tmp_path, "demo").read_text(encoding="utf-8"))
    assert [form["id"] for form in data["forms"]] == ["defn/zeta", "defn/alpha", "defn/beta"]


def test_a_form_without_a_line_sorts_first(tmp_path):
    write_results(tmp_path, "a.go", [dataclasses.replace(_form("demo", "b", "a.go", "h1"), line=1)], {})
    legacy = loads(snapshot_path(tmp_path, "demo").read_text(encoding="utf-8"))
    legacy["forms"][0].pop("line")
    snapshot_path(tmp_path, "demo").write_text(dumps(legacy) + "\n", encoding="utf-8")
    # Another file's run renders a form into a.go, next to the line-less one.
    write_results(tmp_path, "c.go", [dataclasses.replace(_form("demo", "a", "a.go", "h2"), line=1)], {})
    data = loads(snapshot_path(tmp_path, "demo").read_text(encoding="utf-8"))
    assert [form["id"] for form in data["forms"]] == ["defn/b", "defn/a"]


def test_a_namespace_left_by_a_file_keeps_the_other_files_source(tmp_path):
    write_results(tmp_path, "a.go", [_form("demo", "Run", "a.go", "hash-a")], {})
    write_results(tmp_path, "b.go", [_form("demo", "Stop", "b.go", "hash-b")], {})
    write_results(tmp_path, "a.go", [_form("moved", "Run", "a.go", "hash-a")], {})
    data = loads(snapshot_path(tmp_path, "demo").read_text(encoding="utf-8"))
    assert data["source"] == "b.go"
    assert [form["id"] for form in data["forms"]] == ["defn/Stop"]


def test_a_replaced_snapshot_stays_world_readable(tmp_path):
    write_results(tmp_path, "a.go", [_form("demo", "Run", "a.go", "hash-a")], {})
    write_results(tmp_path, "a.go", [_form("demo", "Run", "a.go", "hash-b")], {})
    path = snapshot_path(tmp_path, "demo")
    assert "hash-b" in path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o644
    assert sorted(item.name for item in path.parent.iterdir()) == ["demo.edn"]
