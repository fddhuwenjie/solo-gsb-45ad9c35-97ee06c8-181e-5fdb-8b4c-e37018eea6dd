"""Restricted JSONPath evaluation for ``POST /query``.

Semantics
---------
* A query is a restricted JSONPath expression.  Supported syntax:

  =======================  ===============================================
  ``$``                    the document root
  ``.name``                object member (dot shorthand)
  ``['name']``/``["n"]``   object member in brackets, with backslash
                           escapes (``\\'``, ``\\\\``, ``\\uXXXX`` …)
  ``[0]``                  array index (negative counts from the end)
  ``[*]``                  wildcard: every array element / object member
  ``..name``               recursive descent: member ``name`` of every
                           object in the subtree, the node itself included
  ``..*``                  recursive descent: every descendant node
  ``[?(@.f op literal)]``  array element filter; ``op`` is one of
                           ``== != < <= > >=`` and ``literal`` a JSON
                           scalar (string, number, ``true``/``false``,
                           ``null``)
  =======================  ===============================================

* Filters never match when the field is missing or holds a non-scalar
  (object/array) — not even via ``!=``.  Equality is JSON-type strict
  (``1`` differs from ``true`` and from ``"1"``, but ``1 == 1.0``); the
  ordering operators only compare non-boolean numbers, anything else is
  a non-match (not an error).
* Matches are returned in document pre-order as ``{"path", "value"}``
  pairs.  ``path`` is a JSON Pointer (RFC 6901; ``""`` is the root) and
  each path is reported at most once.
* A single query may visit at most 10 000 nodes.  Malformed expressions,
  duplicated ``query_id`` values and budget overruns are all rejected.
* Evaluation is read-only: the document is never mutated and nothing but
  the grammar above is interpreted — filters compare values, they never
  execute code.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .models import QueryItem

NODE_LIMIT = 10_000

_WS = " \t\n\r"
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_INDEX_RE = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")

# A compiled segment is a tuple:
#   ("child", name)                 .name or ['name']
#   ("index", index)                [i]
#   ("wildcard",)                   [*]
#   ("recursive_name", name)        ..name
#   ("recursive_wildcard",)         ..*
#   ("filter", field, op, literal)  [?(@.field op literal)]
Segment = tuple
# A work item is (value, JSON Pointer, pre-order rank) where the rank is
# the tuple of sibling positions from the root down to the node.
Item = tuple[Any, str, tuple[int, ...]]


class QueryValidationError(ValueError):
    """Raised for malformed JSONPath, duplicate ids or budget overruns."""


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def parse_path(path: str) -> list[Segment]:
    """Parse a restricted JSONPath expression into a list of segments."""
    if not path.startswith("$"):
        raise QueryValidationError(
            f"invalid JSONPath {path!r}: expression must start with '$'"
        )
    segments: list[Segment] = []
    i, n = 1, len(path)
    while i < n:
        ch = path[i]
        if ch == ".":
            if path.startswith("..", i):
                i += 2
                if i < n and path[i] == "*":
                    segments.append(("recursive_wildcard",))
                    i += 1
                else:
                    name, i = _dot_name(path, i)
                    segments.append(("recursive_name", name))
            else:
                name, i = _dot_name(path, i + 1)
                segments.append(("child", name))
        elif ch == "[":
            segment, i = _bracket(path, i)
            segments.append(segment)
        else:
            raise QueryValidationError(
                f"invalid JSONPath {path!r}: unexpected {ch!r} at position {i}"
            )
    return segments


def _is_name_first(ch: str) -> bool:
    return ch == "_" or ch.isalpha() or ord(ch) >= 0x80


def _is_name_char(ch: str) -> bool:
    return _is_name_first(ch) or ch.isdigit()


def _dot_name(path: str, i: int) -> tuple[str, int]:
    if i >= len(path) or not _is_name_first(path[i]):
        raise QueryValidationError(
            f"invalid JSONPath {path!r}: expected a member name at position {i}"
        )
    start = i
    i += 1
    while i < len(path) and _is_name_char(path[i]):
        i += 1
    return path[start:i], i


def _skip_ws(path: str, i: int) -> int:
    while i < len(path) and path[i] in _WS:
        i += 1
    return i


def _expect(path: str, i: int, ch: str) -> int:
    if i >= len(path) or path[i] != ch:
        got = repr(path[i]) if i < len(path) else "end of expression"
        raise QueryValidationError(
            f"invalid JSONPath {path!r}: expected {ch!r} at position {i}, "
            f"got {got}"
        )
    return i + 1


def _bracket(path: str, i: int) -> tuple[Segment, int]:
    """Parse one ``[...]`` selector; ``path[i]`` is the opening bracket."""
    i = _skip_ws(path, i + 1)
    if i >= len(path):
        raise QueryValidationError(f"invalid JSONPath {path!r}: unterminated '['")
    ch = path[i]
    if ch in "'\"":
        key, i = _quoted(path, i)
        i = _expect(path, _skip_ws(path, i), "]")
        return ("child", key), i
    if ch == "*":
        i = _expect(path, _skip_ws(path, i + 1), "]")
        return ("wildcard",), i
    if ch == "?":
        i = _expect(path, _skip_ws(path, i + 1), "(")
        segment, i = _filter(path, i)
        i = _expect(path, _skip_ws(path, i), ")")
        i = _expect(path, _skip_ws(path, i), "]")
        return segment, i
    if ch == "-" or ch.isdigit():
        match = _INDEX_RE.match(path, i)
        if match is None:
            raise QueryValidationError(
                f"invalid JSONPath {path!r}: bad array index at position {i}"
            )
        i = _expect(path, _skip_ws(path, match.end()), "]")
        return ("index", int(match.group(0))), i
    raise QueryValidationError(
        f"invalid JSONPath {path!r}: unsupported selector at position {i}"
    )


def _filter(path: str, i: int) -> tuple[Segment, int]:
    """Parse ``@.field op literal`` (the ``?(`` was already consumed)."""
    i = _expect(path, _skip_ws(path, i), "@")
    i = _expect(path, i, ".")
    field, i = _dot_name(path, i)
    i = _skip_ws(path, i)
    op, i = _operator(path, i)
    i = _skip_ws(path, i)
    literal, i = _literal(path, i)
    return ("filter", field, op, literal), i


def _operator(path: str, i: int) -> tuple[str, int]:
    for op in ("==", "!=", "<=", ">="):
        if path.startswith(op, i):
            return op, i + 2
    if i < len(path) and path[i] in "<>":
        return path[i], i + 1
    raise QueryValidationError(
        f"invalid JSONPath {path!r}: expected one of == != < <= > >= "
        f"at position {i}"
    )


def _literal(path: str, i: int) -> tuple[Any, int]:
    """Parse a JSON scalar literal (string/number/boolean/null)."""
    if i >= len(path):
        raise QueryValidationError(f"invalid JSONPath {path!r}: missing literal")
    ch = path[i]
    if ch == '"':
        return _json_string(path, i)
    if ch == "-" or ch.isdigit():
        match = _NUMBER_RE.match(path, i)
        if match is None:
            raise QueryValidationError(
                f"invalid JSONPath {path!r}: bad number at position {i}"
            )
        text = match.group(0)
        value: Any = (
            float(text) if any(c in text for c in ".eE") else int(text)
        )
        return value, match.end()
    for keyword, value in (("true", True), ("false", False), ("null", None)):
        if path.startswith(keyword, i):
            end = i + len(keyword)
            if end < len(path) and (path[end].isalnum() or path[end] == "_"):
                break
            return value, end
    raise QueryValidationError(
        f"invalid JSONPath {path!r}: literal must be a JSON scalar "
        f"(string, number, true, false or null) at position {i}"
    )


def _json_string(path: str, i: int) -> tuple[str, int]:
    """Parse a double-quoted JSON string literal via ``json.loads``."""
    j, n = i + 1, len(path)
    while j < n:
        ch = path[j]
        if ch == "\\":
            j += 2
            continue
        if ch == '"':
            token = path[i : j + 1]
            try:
                return json.loads(token), j + 1
            except json.JSONDecodeError as exc:
                raise QueryValidationError(
                    f"invalid JSONPath {path!r}: bad string literal ({exc})"
                ) from exc
        j += 1
    raise QueryValidationError(
        f"invalid JSONPath {path!r}: unterminated string literal"
    )


_ESCAPES = {
    "'": "'",
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def _quoted(path: str, i: int) -> tuple[str, int]:
    """Parse a single- or double-quoted bracket key with escapes."""
    quote = path[i]
    i += 1
    n = len(path)
    parts: list[str] = []
    while i < n:
        ch = path[i]
        if ch == quote:
            return "".join(parts), i + 1
        if ch == "\\":
            i += 1
            if i >= n:
                break
            esc = path[i]
            if esc == "u":
                char, i = _unicode_escape(path, i)
                parts.append(char)
                continue
            if esc in _ESCAPES:
                parts.append(_ESCAPES[esc])
                i += 1
                continue
            raise QueryValidationError(
                f"invalid JSONPath {path!r}: unknown escape '\\{esc}' "
                f"at position {i - 1}"
            )
        if ord(ch) < 0x20:
            raise QueryValidationError(
                f"invalid JSONPath {path!r}: control character in quoted name"
            )
        parts.append(ch)
        i += 1
    raise QueryValidationError(
        f"invalid JSONPath {path!r}: unterminated quoted name"
    )


def _unicode_escape(path: str, i: int) -> tuple[str, int]:
    """Decode one ``\\uXXXX`` escape (``path[i] == 'u'``), joining pairs."""
    digits = path[i + 1 : i + 5]
    if len(digits) < 4 or any(c not in _HEX_DIGITS for c in digits):
        raise QueryValidationError(
            f"invalid JSONPath {path!r}: bad \\u escape at position {i - 1}"
        )
    code = int(digits, 16)
    i += 5
    if 0xD800 <= code <= 0xDBFF:
        if path[i : i + 2] == "\\u":
            low_digits = path[i + 2 : i + 6]
            if len(low_digits) == 4 and all(
                c in _HEX_DIGITS for c in low_digits
            ):
                low = int(low_digits, 16)
                if 0xDC00 <= low <= 0xDFFF:
                    combined = (
                        0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    )
                    return chr(combined), i + 6
        raise QueryValidationError(
            f"invalid JSONPath {path!r}: unpaired surrogate in \\u escape"
        )
    if 0xDC00 <= code <= 0xDFFF:
        raise QueryValidationError(
            f"invalid JSONPath {path!r}: unpaired surrogate in \\u escape"
        )
    return chr(code), i


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------

class _Budget:
    """Per-query node visit counter with a hard limit."""

    def __init__(self) -> None:
        self.visited = 0

    def tick(self) -> None:
        self.visited += 1
        if self.visited > NODE_LIMIT:
            raise QueryValidationError(
                f"query visited more than {NODE_LIMIT} nodes"
            )


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _child_item(
    value: Any, pointer: str, rank: tuple[int, ...], key: str, position: int
) -> Item:
    return (value, pointer + "/" + _escape(key), rank + (position,))


def _apply_child(items: list[Item], name: str, budget: _Budget) -> list[Item]:
    out: list[Item] = []
    for value, pointer, rank in items:
        budget.tick()
        if isinstance(value, dict) and name in value:
            for position, key in enumerate(value):
                if key == name:
                    break
            out.append(_child_item(value[name], pointer, rank, name, position))
    return out


def _apply_index(items: list[Item], index: int, budget: _Budget) -> list[Item]:
    out: list[Item] = []
    for value, pointer, rank in items:
        budget.tick()
        if isinstance(value, list):
            resolved = index if index >= 0 else len(value) + index
            if 0 <= resolved < len(value):
                out.append(
                    (value[resolved], pointer + "/" + str(resolved), rank + (resolved,))
                )
    return out


def _apply_wildcard(items: list[Item], budget: _Budget) -> list[Item]:
    out: list[Item] = []
    for value, pointer, rank in items:
        budget.tick()
        if isinstance(value, dict):
            for position, (key, child) in enumerate(value.items()):
                budget.tick()
                out.append(_child_item(child, pointer, rank, key, position))
        elif isinstance(value, list):
            for position, child in enumerate(value):
                budget.tick()
                out.append(
                    (child, pointer + "/" + str(position), rank + (position,))
                )
    return out


def _push_children(
    stack: list[Item], node: Any, pointer: str, rank: tuple[int, ...]
) -> None:
    """Push a node's children onto the DFS stack, reversed for pre-order."""
    if isinstance(node, dict):
        children = list(node.items())
        for position in range(len(children) - 1, -1, -1):
            key, child = children[position]
            stack.append(_child_item(child, pointer, rank, key, position))
    elif isinstance(node, list):
        for position in range(len(node) - 1, -1, -1):
            stack.append(
                (node[position], pointer + "/" + str(position), rank + (position,))
            )


def _apply_recursive_name(
    items: list[Item], name: str, budget: _Budget
) -> list[Item]:
    out: list[Item] = []
    for value, pointer, rank in items:
        stack: list[Item] = [(value, pointer, rank)]
        while stack:
            node, node_ptr, node_rank = stack.pop()
            budget.tick()
            _push_children(stack, node, node_ptr, node_rank)
            if isinstance(node, dict) and name in node:
                position = list(node).index(name)
                out.append(_child_item(node[name], node_ptr, node_rank, name, position))
    return out


def _apply_recursive_wildcard(items: list[Item], budget: _Budget) -> list[Item]:
    out: list[Item] = []
    for value, pointer, rank in items:
        budget.tick()  # the subtree root is visited but not selected
        stack: list[Item] = []
        _push_children(stack, value, pointer, rank)
        while stack:
            node, node_ptr, node_rank = stack.pop()
            budget.tick()
            out.append((node, node_ptr, node_rank))
            _push_children(stack, node, node_ptr, node_rank)
    return out


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "other"


def _json_equal(a: Any, b: Any) -> bool:
    """JSON-typed equality: ``1`` != ``true`` != ``"1"``, but ``1 == 1.0``."""
    return _json_type(a) == _json_type(b) and a == b


def _compare(field_value: Any, op: str, literal: Any) -> bool:
    if op == "==":
        return _json_equal(field_value, literal)
    if op == "!=":
        return not _json_equal(field_value, literal)
    # ordering operators only compare non-boolean numbers
    if _json_type(field_value) != "number" or _json_type(literal) != "number":
        return False
    if op == "<":
        return field_value < literal
    if op == "<=":
        return field_value <= literal
    if op == ">":
        return field_value > literal
    return field_value >= literal


def _apply_filter(
    items: list[Item], field: str, op: str, literal: Any, budget: _Budget
) -> list[Item]:
    out: list[Item] = []
    for value, pointer, rank in items:
        budget.tick()
        if not isinstance(value, list):
            continue  # filters select array elements only
        for position, element in enumerate(value):
            budget.tick()
            if not isinstance(element, dict) or field not in element:
                continue  # missing field never matches
            field_value = element[field]
            if isinstance(field_value, (dict, list)):
                continue  # non-scalar field never matches
            if _compare(field_value, op, literal):
                out.append(
                    (element, pointer + "/" + str(position), rank + (position,))
                )
    return out


def _apply_segment(items: list[Item], segment: Segment, budget: _Budget) -> list[Item]:
    kind = segment[0]
    if kind == "child":
        return _apply_child(items, segment[1], budget)
    if kind == "index":
        return _apply_index(items, segment[1], budget)
    if kind == "wildcard":
        return _apply_wildcard(items, budget)
    if kind == "recursive_name":
        return _apply_recursive_name(items, segment[1], budget)
    if kind == "recursive_wildcard":
        return _apply_recursive_wildcard(items, budget)
    if kind == "filter":
        _, field, op, literal = segment
        return _apply_filter(items, field, op, literal, budget)
    raise AssertionError(f"unknown segment {segment!r}")  # pragma: no cover


def execute(document: Any, segments: list[Segment]) -> list[dict[str, Any]]:
    """Evaluate parsed segments and return pre-ordered ``{path, value}`` rows."""
    budget = _Budget()
    items: list[Item] = [(document, "", ())]
    for segment in segments:
        items = _apply_segment(items, segment, budget)
        if not items:
            break
    seen: set[str] = set()
    unique: list[Item] = []
    for value, pointer, rank in items:
        if pointer not in seen:
            seen.add(pointer)
            unique.append((value, pointer, rank))
    unique.sort(key=lambda item: item[2])
    # evaluation is read-only, so matched values can be referenced directly
    # (a deepcopy would also recurse past Python's limit on deep documents)
    return [{"path": pointer, "value": value} for value, pointer, _ in unique]


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def run_queries(document: Any, queries: list[QueryItem]) -> dict[str, Any]:
    """Validate and evaluate every query against the same document."""
    seen_ids: set[str] = set()
    for q in queries:
        if q.query_id in seen_ids:
            raise QueryValidationError(f"duplicate query_id {q.query_id!r}")
        seen_ids.add(q.query_id)

    results: list[dict[str, Any]] = []
    for q in queries:
        try:
            segments = parse_path(q.path)
            matches = execute(document, segments)
        except QueryValidationError as exc:
            raise QueryValidationError(f"query {q.query_id!r}: {exc}") from exc
        results.append({"query_id": q.query_id, "matches": matches})
    return {"results": results}
