"""Read and write the EDN subset uml-viewer loads with clojure.edn."""

from __future__ import annotations

import re


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _keyword_key(key: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z*+!_?][A-Za-z0-9*+!_?\-./]*", key))


def dumps(value) -> str:
    """Render a Python value as EDN.

    Dict keys that are plain identifiers become keywords. Other keys and all
    ordinary strings become EDN strings. `True` and `False` are not used;
    booleans here are the strings `true` and `false` only when passed through
    `keyword`.
    """

    return _render(value)


def keyword(name: str) -> tuple:
    """A value that renders as an EDN keyword."""

    return ("keyword", name)


def _float(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def _vector(value) -> str:
    return "[" + " ".join(_render(item) for item in value) + "]"


def _map_key(key) -> str:
    if isinstance(key, str) and _keyword_key(key):
        return f":{key}"
    return _render(key)


def _map(value: dict) -> str:
    return "{" + " ".join(f"{_map_key(key)} {_render(item)}" for key, item in value.items()) + "}"


# bool before int: True is an int.
_RENDERERS = (
    (bool, lambda value: "true" if value else "false"),
    (int, str),
    (float, _float),
    (str, lambda value: f'"{_escape(value)}"'),
    ((list, tuple), _vector),
    (dict, _map),
)


def _render(value) -> str:
    if value is None:
        return "nil"
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "keyword":
        return f":{value[1]}"
    for kind, render in _RENDERERS:
        if isinstance(value, kind):
            return render(value)
    raise TypeError(f"cannot render {type(value).__name__} as EDN")


class _Reader:
    def __init__(self, text: str):
        self.text = text
        self.length = len(text)
        self.index = 0

    def parse(self):
        self._skip()
        if self.index >= self.length:
            raise ValueError("empty EDN")
        value = self._value()
        self._skip()
        if self.index < self.length:
            raise ValueError(f"trailing EDN at {self.index}")
        return value

    def _peek(self) -> str:
        if self.index >= self.length:
            raise ValueError("unexpected end of EDN")
        return self.text[self.index]

    def _skip(self) -> None:
        while self.index < self.length:
            ch = self.text[self.index]
            if ch.isspace() or ch == ",":
                self.index += 1
                continue
            if ch == ";":
                while self.index < self.length and self.text[self.index] != "\n":
                    self.index += 1
                continue
            break

    def _value(self):
        self._skip()
        ch = self._peek()
        if ch == '"':
            return self._string()
        if ch == ":":
            return self._keyword()
        if ch == "{":
            return ("map", self._collection("}", self._map_item))
        if ch == "[":
            return ("vec", self._collection("]", self._element))
        if ch == "(":
            return ("list", self._collection(")", self._element))
        if ch == "#":
            self.index += 1
            if self._peek() == "{":
                return set(self._collection("}", self._element))
            raise ValueError("unsupported EDN dispatch")
        return self._atom()

    def _collection(self, closing: str, item):
        self.index += 1
        values = []
        while True:
            self._skip()
            if self._peek() == closing:
                self.index += 1
                return values
            values.append(item())

    def _element(self):
        return self._value()

    def _map_item(self):
        key = self._value()
        self._skip()
        val = self._value()
        return key, val

    def _string(self) -> str:
        self.index += 1
        chars: list[str] = []
        while self.index < self.length:
            ch = self.text[self.index]
            self.index += 1
            if ch == '"':
                return "".join(chars)
            if ch == "\\":
                if self.index >= self.length:
                    raise ValueError("unterminated string escape")
                escaped = self.text[self.index]
                self.index += 1
                chars.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(escaped, escaped))
                continue
            chars.append(ch)
        raise ValueError("unterminated string")

    def _keyword(self) -> str:
        self.index += 1
        start = self.index
        while self.index < self.length and _symbol_char(self.text[self.index]):
            self.index += 1
        if self.index == start:
            raise ValueError("empty keyword")
        return self.text[start : self.index]

    def _atom(self):
        start = self.index
        while self.index < self.length and _symbol_char(self.text[self.index]):
            self.index += 1
        token = self.text[start : self.index]
        if token == "":
            raise ValueError(f"unexpected character {self._peek()!r}")
        if token == "nil":
            return None
        if token == "true":
            return True
        if token == "false":
            return False
        if re.fullmatch(r"-?\d+", token):
            return int(token)
        if re.fullmatch(r"-?\d+\.\d+", token):
            return float(token)
        return token


def _symbol_char(ch: str) -> bool:
    return ch not in ' \t\r\n,{}[]()"\\;'


def _maps(value):
    if isinstance(value, tuple) and len(value) == 2 and value[0] in {"map", "vec", "list"}:
        kind, items = value
        if kind == "map":
            return {_key(key): _maps(item) for key, item in items}
        return [_maps(item) for item in items]
    if isinstance(value, set):
        return {_maps(item) for item in value}
    return value


def _key(value):
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return str(value)


def loads(text: str):
    """Parse one EDN value. Keywords become their names, without the colon."""

    return _maps(_Reader(text).parse())
