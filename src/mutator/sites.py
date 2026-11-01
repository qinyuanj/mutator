"""Find mutation sites.

Clojure follows clj-mutate's symbol rules. Go follows mutate4go, including
one-way `*` to `/`. Java follows mutate4java. TypeScript, Rust, and Python
extend Java's decisions with bitwise, shift, and remainder swaps. Lua spells
inequality `~=`, mutates `//` one way to `/`, and leaves `..` and `#` alone.
"""

from __future__ import annotations

from mutator.crapper_link import ensure_crapper

_BINARY = {
    "+": "-",
    "-": "+",
    "*": "/",
    "/": "*",
    ">": ">=",
    ">=": ">",
    "<": "<=",
    "<=": "<",
    "==": "!=",
    "!=": "==",
    "===": "!==",
    "!==": "===",
    "&&": "||",
    "||": "&&",
    "and": "or",
    "or": "and",
}

_BITWISE_BINARY = {"^": "|", "&": "|", "|": "&", "<<": ">>", ">>": "<<", "%": "*"}

_LUA_BINARY = {**_BINARY, "==": "~=", "~=": "==", "//": "/"}

_BINARY_PARENTS = {
    "binary_expression",
    "binary_operator",
    "comparison_operator",
    "boolean_operator",
}

_UNARY_PARENTS = {"unary_expression", "unary_operator", "not_operator"}

_BOOLEANS = {"true": "false", "false": "true", "True": "False", "False": "True"}

_NUMBERS = {
    "decimal_integer_literal",
    "integer_literal",
    "int_literal",
    "integer",
    "number",
    "num_lit",
}

_CLOJURE_HEAD = {
    "+": ("-", "arithmetic"),
    "-": ("+", "arithmetic"),
    "*": ("/", "arithmetic"),
    "inc": ("dec", "arithmetic"),
    "dec": ("inc", "arithmetic"),
    ">": (">=", "comparison"),
    ">=": (">", "comparison"),
    "<": ("<=", "comparison"),
    "<=": ("<", "comparison"),
    "=": ("not=", "equality"),
    "not=": ("=", "equality"),
    "if": ("if-not", "conditional"),
    "if-not": ("if", "conditional"),
    "when": ("when-not", "conditional"),
    "when-not": ("when", "conditional"),
    "and": ("or", "logical"),
    "or": ("and", "logical"),
    "double": ("int", "coercion"),
    "int": ("double", "coercion"),
    "first": ("second", "seq"),
    "second": ("first", "seq"),
    "filter": ("remove", "seq"),
    "remove": ("filter", "seq"),
    "take": ("drop", "seq"),
    "drop": ("take", "seq"),
    "rest": ("next", "seq"),
    "next": ("rest", "seq"),
    "every?": ("some", "seq"),
    "some": ("every?", "seq"),
    "min": ("max", "numeric"),
    "max": ("min", "numeric"),
    "pos?": ("neg?", "predicate"),
    "neg?": ("pos?", "predicate"),
    "even?": ("odd?", "predicate"),
    "odd?": ("even?", "predicate"),
    "nil?": ("some?", "predicate"),
    "some?": ("nil?", "predicate"),
}


class RawSite:
    def __init__(self, line: int, start: int, end: int, original: str, mutant: str, category: str):
        self.line = line
        self.start = start
        self.end = end
        self.original = original
        self.mutant = mutant
        self.category = category


def _grammar(language: str, path: str) -> str:
    if language == "typescript" and path.endswith(".tsx"):
        return "tsx"
    if language == "go":
        return "go"
    return language


def _text(data: bytes, node) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")


def _category(token: str) -> str:
    if token in {"+", "-", "*", "/"}:
        return "arithmetic"
    if token in {">", ">=", "<", "<="}:
        return "comparison"
    if token in {"==", "!=", "===", "!==", "~="}:
        return "equality"
    if token in {"^", "&", "|", "<<", ">>", ">>>"}:
        return "bitwise"
    if token in {"&&", "||", "and", "or"}:
        return "logical"
    return "arithmetic"


def _binary_mutant(language: str, token: str) -> str | None:
    if language == "go" and token == "/":
        return None
    if language == "lua":
        return _LUA_BINARY.get(token)
    if language == "typescript" and token == ">>>":
        return "<<"
    if language in {"rust", "typescript", "python"} and token in _BITWISE_BINARY:
        return _BITWISE_BINARY[token]
    return _BINARY.get(token)


def _add(found: list[RawSite], node, original: str, mutant: str, category: str) -> None:
    if mutant == original:
        return
    found.append(
        RawSite(
            line=node.start_point[0] + 1,
            start=node.start_byte,
            end=node.end_byte,
            original=original,
            mutant=mutant,
            category=category,
        )
    )


def _clojure_head(node, data: bytes) -> str | None:
    for child in node.children:
        if child.type == "sym_lit":
            return _text(data, child)
    return None


def _is_head(node) -> bool:
    parent = node.parent
    if parent is None or parent.type != "list_lit":
        return False
    for child in parent.children:
        if child.type == "sym_lit":
            return child.id == node.id
    return False


def _clojure_skipped(node, data: bytes) -> bool:
    current = node
    while current is not None:
        if any(part in current.type for part in ("comment", "quot", "string", "str_lit", "char_lit")):
            return True
        if current.type == "list_lit":
            head = _clojure_head(current, data)
            if head in {"comment", "quote"}:
                return True
        current = current.parent
    return False


def _clojure_text(node, data: bytes) -> str:
    if node.type in {"sym_lit", "bool_lit", "num_lit"}:
        return _text(data, node)
    return ""


def _flip01(text: str) -> str:
    if text == "0":
        return "1"
    return "0"


def _clojure_node(node, data: bytes, found: list[RawSite]) -> None:
    if _clojure_skipped(node, data):
        return
    text = _clojure_text(node, data)
    if node.type == "sym_lit" and _is_head(node) and text in _CLOJURE_HEAD:
        mutant, category = _CLOJURE_HEAD[text]
        _add(found, node, text, mutant, category)
        return
    if node.type == "bool_lit":
        _add(found, node, text, _BOOLEANS[text], "boolean")
        return
    if node.type == "num_lit" and text in {"0", "1"}:
        _add(found, node, text, _flip01(text), "constant")


def _visit_clojure(node, data: bytes, found: list[RawSite]) -> None:
    _clojure_node(node, data, found)
    for child in node.children:
        _visit_clojure(child, data, found)


def _python_annotation(node) -> bool:
    current = node.parent
    while current is not None:
        if current.type == "type":
            return True
        current = current.parent
    return False


def _binary_site(node, token: str, language: str) -> str | None:
    mutant = _binary_mutant(language, token)
    # Python spells union annotations with the same binary AST as `a | b`.
    if language == "python" and token in _BITWISE_BINARY and _python_annotation(node):
        return None
    return mutant


def _operator(node, token: str, parent: str, language: str, found: list[RawSite]) -> None:
    if parent in _BINARY_PARENTS:
        mutant = _binary_site(node, token, language)
        if mutant is not None:
            _add(found, node, token, mutant, _category(token))
    if token in {"!", "not"} and parent in _UNARY_PARENTS | {"comparison_operator"}:
        _add(found, node, token, "", "unary")
    if token == "-" and parent in _UNARY_PARENTS:
        _add(found, node, token, "", "unary")


def _literal(node, token: str, found: list[RawSite]) -> None:
    if node.type in {"true", "false"} and token in _BOOLEANS:
        _add(found, node, token, _BOOLEANS[token], "boolean")
    if node.type in _NUMBERS and token in {"0", "1"}:
        _add(found, node, token, _flip01(token), "constant")


def _visit(node, data: bytes, language: str, found: list[RawSite]) -> None:
    if node.child_count == 0:
        token = _text(data, node)
        parent = node.parent.type if node.parent is not None else ""
        # A literal sits inside the same expression as an operator, so these
        # checks are independent. `0` is a child of `a > 0`, not an operator.
        _operator(node, token, parent, language, found)
        _literal(node, token, found)
    for child in node.children:
        _visit(child, data, language, found)


def discover_raw(source: str, language: str, path: str) -> list[RawSite]:
    """Mutation sites in source order. Offsets are UTF-8 byte offsets."""

    crapper = ensure_crapper()
    data = source.encode("utf-8")
    grammar = _grammar(language, path)
    _data, tree = crapper.languages.treesitter.parse(source, grammar)
    found: list[RawSite] = []
    if language == "clojure":
        _visit_clojure(tree.root_node, data, found)
    else:
        _visit(tree.root_node, data, language, found)
    found.sort(key=lambda site: (site.line, site.start, site.end))
    return found


def apply_site(source: str, start: int, end: int, mutant: str) -> str:
    data = source.encode("utf-8")
    updated = data[:start] + mutant.encode("utf-8") + data[end:]
    return updated.decode("utf-8")
