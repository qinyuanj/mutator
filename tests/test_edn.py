import pytest

from mutator.edn import dumps, keyword, loads


def test_round_trip_matches_the_snapshot_shape():
    payload = {
        "version": 1,
        "tested-at": "2026-09-30T12:00:00-05:00",
        "source": 'src/demo/say "hi".py',
        "namespace": "demo.Board",
        "outcomes": {
            '["src/a.py","demo.Board","defn/place",1,2,">",">="]': keyword("killed"),
            '["src/a.py","demo.Board","defn-/hide",3,4,"true","false"]': keyword("survived"),
        },
        "forms": [
            {
                "id": "defn/place",
                "kind": "defn",
                "file": "src/demo/Board.java",
                "line": 4,
                "end-line": 9,
                "hash": "abc",
                "killed": 1,
                "survived": 0,
                "uncovered": 2,
                "sites": 3,
            }
        ],
    }
    text = dumps(payload)
    assert text.startswith("{")
    assert ":namespace" in text
    assert ":killed" in text
    parsed = loads(text)
    assert parsed["namespace"] == "demo.Board"
    assert parsed["forms"][0]["id"] == "defn/place"
    assert parsed["forms"][0]["end-line"] == 9
    assert parsed["forms"][0]["killed"] == 1
    assert set(parsed["outcomes"].values()) == {"killed", "survived"}


def test_empty_collections_and_nil():
    assert loads("{:forms [] :outcomes {} :note nil}") == {
        "forms": [],
        "outcomes": {},
        "note": None,
    }


def test_atoms_cover_booleans_numbers_and_symbols():
    assert loads("true") is True
    assert loads("false") is False
    assert loads("12") == 12
    assert loads("-3") == -3
    assert loads("1.50") == 1.5
    assert loads("foo") == "foo"


def test_dumps_renders_every_supported_value():
    assert dumps(None) == "nil"
    assert dumps(True) == "true"
    assert dumps(False) == "false"
    assert dumps(0) == "0"
    assert dumps(-7) == "-7"
    assert dumps(1.5) == "1.5"
    assert dumps(2.0) == "2.0"
    assert dumps(0.123456) == "0.1235"
    assert dumps('a "b" \\c') == '"a \\"b\\" \\\\c"'
    assert dumps(keyword("killed")) == ":killed"
    assert dumps([]) == "[]"
    assert dumps(()) == "[]"
    assert dumps([1, ("x", 2), [True]]) == '[1 ["x" 2] [true]]'
    assert dumps({}) == "{}"
    assert dumps({"end-line": 9, "a b": 1, 3: None}) == '{:end-line 9 "a b" 1 3 nil}'


def test_dumps_rejects_values_edn_cannot_hold():
    with pytest.raises(TypeError, match="cannot render set as EDN"):
        dumps({1})
