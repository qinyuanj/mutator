from pathlib import Path

import pytest

from mutator.crapper_link import ensure_crapper
from mutator.functions import form_id, is_private, project_functions, sites_in_file
from mutator.sites import apply_site, discover_raw


def _sites(tmp_path: Path, relative: str, source: str):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return sites_in_file(source, path, tmp_path, relative)


def test_clojure_rules_follow_clj_mutate_and_mark_private_defns(tmp_path):
    source = """(ns demo.core)
(defn place [x]
  (if (> x 0)
    (+ x 1)
    0))
(defn- hide []
  false)
"""
    sites = _sites(tmp_path, "src/demo/core.clj", source)
    described = {(site.form_id, site.original, site.mutant) for site in sites}
    assert ("defn/place", "if", "if-not") in described
    assert ("defn/place", ">", ">=") in described
    assert ("defn/place", "+", "-") in described
    assert ("defn/place", "0", "1") in described
    assert ("defn/place", "1", "0") in described
    assert ("defn-/hide", "false", "true") in described
    assert {site.namespace for site in sites} == {"demo.core"}
    functions = project_functions(source, tmp_path / "src/demo/core.clj", tmp_path)
    assert form_id("hide", True) == "defn-/hide"
    assert is_private("clojure", "hide", source, 6, 7)
    assert {fn.namespace for fn in functions} == {"demo.core"}


def test_clojure_ignores_strings_comments_and_quotes(tmp_path):
    source = """(ns demo.core)
(defn place []
  (comment (+ 1 0))
  (quote (+ 1 0))
  '(+ 1 0)
  `(+ 1 0)
  ; (+ 1 0)
  (str "true + 1"))
"""
    sites = _sites(tmp_path, "src/demo/core.clj", source)
    assert sites == []


def test_java_rules_follow_mutate4java(tmp_path):
    source = """package demo;
class Board {
  int place(int x) {
    if (x > 0 && ready)
      return x + 1;
    return !ok ? -x : 0;
  }
  private int hide() { return 1; }
}
"""
    sites = _sites(tmp_path, "src/demo/Board.java", source)
    public = [site for site in sites if site.form_id == "defn/place"]
    private = [site for site in sites if site.form_id == "defn-/hide"]
    assert ("+", "-") in {(site.original, site.mutant) for site in public}
    assert ("/", "*") not in {(site.original, site.mutant) for site in public}
    assert ("*", "/") not in {(site.original, site.mutant) for site in public}
    assert (">", ">=") in {(site.original, site.mutant) for site in public}
    assert ("&&", "||") in {(site.original, site.mutant) for site in public}
    assert ("!", "") in {(site.original, site.mutant) for site in public}
    assert ("-", "") in {(site.original, site.mutant) for site in public}
    assert ("0", "1") in {(site.original, site.mutant) for site in public}
    assert private == [] or {site.original for site in private} == {"1"}
    assert {site.namespace for site in sites} == {"demo.Board"}


def test_java_swaps_division_both_ways(tmp_path):
    source = """package demo;
class Math {
  int place(int a, int b) { return a * b + a / b; }
}
"""
    sites = _sites(tmp_path, "src/demo/Math.java", source)
    pairs = {(site.original, site.mutant) for site in sites}
    assert ("*", "/") in pairs
    assert ("/", "*") in pairs
    assert ("+", "-") in pairs


def test_go_matches_mutate4go_and_splits_receivers(tmp_path):
    source = """package demo
func Run(a int, b int) int {
    if a > 0 && b == 1 {
        return a * b
    }
    return a / b
}
type Widget struct{}
func (w Widget) Run() bool { return true }
"""
    sites = _sites(tmp_path, "demo.go", source)
    free = [site for site in sites if site.namespace == "demo"]
    method = [site for site in sites if site.namespace == "demo.Widget"]
    pairs = {(site.original, site.mutant) for site in free}
    assert ("*", "/") in pairs
    assert ("/", "*") not in pairs
    assert ("&&", "||") in pairs
    assert ("==", "!=") in pairs
    assert (">", ">=") in pairs
    assert ("0", "1") in pairs
    assert ("1", "0") in pairs
    assert {site.form_id for site in method} == {"defn/Run"}
    assert ("true", "false") in {(site.original, site.mutant) for site in method}
    assert {site.form_id for site in free} == {"defn/Run"}


def test_go_unexported_function_is_private(tmp_path):
    source = """package demo
func hide() bool { return false }
"""
    sites = _sites(tmp_path, "demo.go", source)
    assert sites[0].form_id == "defn-/hide"


def test_python_typescript_and_rust_use_the_java_decisions(tmp_path):
    python = _sites(
        tmp_path,
        "src/demo/app.py",
        "def place(x):\n    if x > 0 and ready:\n        return x + 1\n    if not ok:\n        return 0\n",
    )
    typescript = _sites(
        tmp_path,
        "src/demo/app.ts",
        "export function place(x: number) {\n  if (x > 0 && ready) return x + 1;\n  return true === false;\n}\n",
    )
    rust = _sites(
        tmp_path,
        "src/lib.rs",
        "fn place(x: i32) -> i32 {\n    if x > 0 && ready { return x + 1; }\n    if !ok { return -x; }\n    0\n}\n",
    )
    assert ("and", "or") in {(site.original, site.mutant) for site in python}
    assert ("not", "") in {(site.original, site.mutant) for site in python}
    assert ("True", "False") not in {(site.original, site.mutant) for site in python}
    assert {site.namespace for site in python} == {"demo.app"}
    assert ("===", "!==") in {(site.original, site.mutant) for site in typescript}
    assert ("&&", "||") in {(site.original, site.mutant) for site in typescript}
    assert {site.namespace for site in typescript} == {"demo.app"}
    assert ("&&", "||") in {(site.original, site.mutant) for site in rust}
    assert ("!", "") in {(site.original, site.mutant) for site in rust}
    assert ("-", "") in {(site.original, site.mutant) for site in rust}


def test_python_private_and_class_namespace(tmp_path):
    source = """class Board:
    def place(self, x):
        return x > 0
    def _hide(self):
        return False
"""
    sites = _sites(tmp_path, "src/demo/board.py", source)
    assert {site.form_id for site in sites if site.name == "place"} == {"defn/place"}
    assert {site.form_id for site in sites if site.name == "_hide"} == {"defn-/_hide"}
    assert {site.namespace for site in sites} == {"demo.board.Board"}


def test_string_operators_are_not_sites(tmp_path):
    source = 'def place():\n    return "x > 0 and true"\n'
    assert _sites(tmp_path, "src/demo/app.py", source) == []


@pytest.mark.parametrize(
    "language,path,source",
    [
        ("rust", "src/lib.rs", "pub fn mix(a: u64, b: u64) -> u64 {\n    ((a ^ b) & (a | b)) << ((a >> b) % b)\n}\n"),
        ("typescript", "src/mix.ts", "export function mix(a: number, b: number) {\n  return ((a ^ b) & (a | b)) << ((a >> b) % b);\n}\n"),
        ("python", "src/mix.py", "def mix(a, b):\n    return ((a ^ b) & (a | b)) << ((a >> b) % b)\n"),
    ],
)
def test_bitwise_shift_and_remainder_sites(tmp_path, language, path, source):
    # Exact source-order expectations independent of the implementation's table.
    sites = _sites(tmp_path, path, source)
    assert [(s.original, s.mutant, s.category) for s in sites] == [
        ("^", "|", "bitwise"),
        ("&", "|", "bitwise"),
        ("|", "&", "bitwise"),
        ("<<", ">>", "bitwise"),
        (">>", "<<", "bitwise"),
        ("%", "*", "arithmetic"),
    ]
    assert {s.name for s in sites} == {"mix"}
    assert {s.line for s in sites} == {2}
    for site in sites:
        assert source.encode()[site.start:site.end].decode() == site.original
        mutant = apply_site(source, site.start, site.end, site.mutant)
        _, tree = ensure_crapper().languages.treesitter.parse(mutant, language)
        assert not tree.root_node.has_error


@pytest.mark.parametrize(
    "expression,original_result,mutant_result",
    [
        ("6 ^ 3", 5, 7),
        ("6 & 3", 2, 7),
        ("6 | 3", 7, 2),
        ("8 << 2", 32, 2),
        ("8 >> 2", 2, 32),
        ("8 % 3", 2, 24),
    ],
)
def test_python_operator_mutants_change_observable_results(expression, original_result, mutant_result):
    site, = discover_raw(expression, "python", "mix.py")
    mutant = apply_site(expression, site.start, site.end, site.mutant)
    assert eval(expression, {"__builtins__": {}}) == original_result
    assert eval(mutant, {"__builtins__": {}}) == mutant_result


@pytest.mark.parametrize("extension", ["ts", "tsx"])
def test_typescript_unsigned_shift_has_a_single_site(tmp_path, extension):
    source = "export function mix(a: number, b: number) { return a >>> b; }\n"
    sites = _sites(tmp_path, f"src/mix.{extension}", source)
    assert [(s.original, s.mutant, s.category) for s in sites] == [
        (">>>", "<<", "bitwise")
    ]
    assert apply_site(source, sites[0].start, sites[0].end, sites[0].mutant) == (
        "export function mix(a: number, b: number) { return a << b; }\n"
    )


@pytest.mark.parametrize(
    "language,path,source",
    [
        ("java", "Mix.java", "class Mix { int mix(int a, int b) { return ((a ^ b) & (a | b)) << ((a >> b) % b); } }"),
        ("go", "mix.go", "package mix\nfunc Mix(a int, b int) int { return ((a ^ b) & (a | b)) << ((a >> b) % b) }"),
        ("lua", "mix.lua", "function mix(a, b) return ((a ~ b) & (a | b)) << ((a >> b) % b) end"),
        ("clojure", "mix.clj", "(ns mix)\n(defn mix [a b] (bit-xor (bit-and a b) (bit-or a b)))"),
    ],
)
def test_new_swaps_do_not_expand_other_language_rules(language, path, source):
    assert discover_raw(source, language, path) == []


@pytest.mark.parametrize(
    "language,path,source",
    [
        ("rust", "mix.rs", 'fn mix(mut a: u64, b: u64) { let s = "^ & | << >> %"; /* ^ & | << >> % */ a ^= b; a &= b; a |= b; a <<= b; a >>= b; a %= b; }'),
        ("typescript", "mix.ts", 'function mix(a: number, b: number) { const s = "^ & | << >> %"; /* ^ & | << >> % */ a ^= b; a &= b; a |= b; a <<= b; a >>= b; a %= b; }'),
        ("python", "mix.py", 'def mix(a, b):\n    s = "^ & | << >> %"\n    # ^ & | << >> %\n    a ^= b\n    a &= b\n    a |= b\n    a <<= b\n    a >>= b\n    a %= b\n'),
    ],
)
def test_new_swaps_only_apply_to_binary_expressions(language, path, source):
    assert discover_raw(source, language, path) == []


@pytest.mark.parametrize(
    "language,path,source",
    [
        ("python", "mix.py", "def identity(x: int | None) -> int | None:\n    y: int | None = x\n    return y\n"),
        ("rust", "mix.rs", "pub fn borrow(x: &Vec<Vec<u64>>) -> &Vec<Vec<u64>> { &*x }\n"),
        ("typescript", "mix.ts", "export function identity(x: number | null): number | null { return x; }\n"),
    ],
)
def test_type_unions_references_and_nested_generics_are_not_bitwise_sites(language, path, source):
    _, tree = ensure_crapper().languages.treesitter.parse(source, language)
    assert not tree.root_node.has_error
    assert discover_raw(source, language, path) == []


def test_python_union_annotation_does_not_hide_runtime_bitwise_site(tmp_path):
    source = "def mask(x: int | None, y: int) -> int | None:\n    return x | y\n"
    sites = _sites(tmp_path, "src/mix.py", source)
    assert [(s.original, s.mutant, s.line) for s in sites] == [("|", "&", 2)]


def test_new_operator_offsets_are_utf8_bytes(tmp_path):
    source = 'def mask(a, b):\n    label = "雪"\n    return a ^ b\n'
    site, = _sites(tmp_path, "src/mix.py", source)
    assert apply_site(source, site.start, site.end, site.mutant) == (
        'def mask(a, b):\n    label = "雪"\n    return a | b\n'
    )


@pytest.mark.parametrize(
    "path,source,expected",
    [
        ("src/lib.rs", "pub fn zero() -> i32 { return 0; }\npub fn one() -> i32 { 1 }\npub fn yes() -> bool { true }\npub fn no() -> bool { return false; }\n", [("0", "1"), ("1", "0"), ("true", "false"), ("false", "true")]),
        ("src/mix.ts", "export function zero() { return 0; }\nexport function one() { return 1; }\nexport function yes() { return true; }\nexport function no() { return false; }\n", [("0", "1"), ("1", "0"), ("true", "false"), ("false", "true")]),
        ("src/mix.py", "def zero():\n    return 0\ndef one():\n    return 1\ndef yes():\n    return True\ndef no():\n    return False\n", [("0", "1"), ("1", "0"), ("True", "False"), ("False", "True")]),
    ],
)
def test_literal_returns_use_existing_sites_without_body_replacement(tmp_path, path, source, expected):
    sites = _sites(tmp_path, path, source)
    assert [(s.original, s.mutant) for s in sites] == expected
    assert [s.name for s in sites] == ["zero", "one", "yes", "no"]


@pytest.mark.parametrize(
    "path,source",
    [
        ("src/lib.rs", "pub fn identity<T>(x: T) -> T { x }\n"),
        ("src/mix.ts", "export function identity<T>(x: T): T { return x; }\n"),
        ("src/mix.py", "def identity(x):\n    return x\n"),
    ],
)
def test_zero_site_functions_are_discovered_without_inventing_returns(tmp_path, path, source):
    assert _sites(tmp_path, path, source) == []
    functions = project_functions(source, tmp_path / path, tmp_path)
    assert [fn.name for fn in functions] == ["identity"]
