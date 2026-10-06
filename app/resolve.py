"""Cross-document JSON reference resolution for ``POST /resolve``.

Semantics
---------
* A request carries 1..50 documents, each with a unique ``document_id``,
  plus a ``root`` reference.  References have the form
  ``document_id#/JSON/Pointer`` where the pointer follows RFC 6901.  The
  ``root`` may omit the pointer — ``"base"`` addresses the whole
  document — but must name a document; nested ``$ref`` values must
  contain ``#`` and may omit the document id, in which case
  ``#/JSON/Pointer`` addresses the *current* document.
* An object containing ``$ref`` is resolved by first resolving the
  target recursively; the remaining keys then form an overlay on the
  resolved target: two objects merge recursively, while arrays and
  scalars are replaced wholesale by the overlay value.  Overlay keys on
  a non-object target are rejected.
* Cycles are detected along the full reference chain: resolving a node
  that is already being resolved upstream is an error.  Missing
  documents, invalid pointers, missing nodes and overlays on non-object
  targets are likewise rejected — always with the reference chain.
* The response carries the materialized document, the ``dependencies``
  (document ids in first-visit order, duplicates removed) and
  ``sources``: a mapping from every leaf's output JSON Pointer to the
  reference it originates from, sorted by output pointer (empty objects
  and arrays count as leaves).  Input documents are never mutated.
"""
from __future__ import annotations

import re
from typing import Any

from .models import ResolveDocument

_INDEX_RE = re.compile(r"0|[1-9][0-9]*")

# A resolution key identifies one node: (document_id, unescaped pointer tokens).
Key = tuple[str, tuple[str, ...]]
# Provenance mirrors the resolved value tree: (source label, children) where
# children is None for scalars, a list for arrays and a dict for objects.
Prov = tuple[str, Any]


class ResolveValidationError(ValueError):
    """Raised for bad references, unresolvable nodes or reference cycles."""


# ---------------------------------------------------------------------------
# JSON Pointer helpers (RFC 6901)
# ---------------------------------------------------------------------------

def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _render(tokens: tuple[str, ...]) -> str:
    return "".join("/" + _escape(t) for t in tokens)


def parse_pointer(pointer: str) -> tuple[str, ...]:
    """Parse and validate an RFC 6901 pointer (``""`` addresses the root)."""
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise ResolveValidationError(f"invalid JSON Pointer {pointer!r}")
    tokens: list[str] = []
    for raw in pointer.split("/")[1:]:
        tokens.append(_unescape(raw, pointer))
    return tuple(tokens)


def _unescape(raw: str, pointer: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "~":
            if i + 1 >= len(raw) or raw[i + 1] not in "01":
                raise ResolveValidationError(
                    f"invalid JSON Pointer {pointer!r}: "
                    f"'~' must be followed by '0' or '1'"
                )
            out.append("~" if raw[i + 1] == "0" else "/")
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _array_index(token: str) -> int | None:
    return int(token) if _INDEX_RE.fullmatch(token) else None


# ---------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------

def _split_reference(ref: str) -> tuple[str, str | None]:
    """Split ``document_id#/pointer``; the pointer is None without ``#``."""
    if "#" in ref:
        doc_id, pointer = ref.split("#", 1)
        return doc_id, pointer
    return ref, None


# ---------------------------------------------------------------------------
# overlay merge
# ---------------------------------------------------------------------------

def _deep_merge(base: Any, overlay: Any) -> Any:
    """Merge overlay onto base: objects recurse, anything else is replaced."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            merged[key] = _deep_merge(merged[key], value) if key in merged else value
        return merged
    return overlay


def _merge_prov(base: Prov, overlay: Prov) -> Prov:
    """Merge provenance trees with exactly the same rules as the values."""
    base_label, base_children = base
    _, overlay_children = overlay
    if isinstance(base_children, dict) and isinstance(overlay_children, dict):
        merged = dict(base_children)
        for key, sub in overlay_children.items():
            merged[key] = _merge_prov(merged[key], sub) if key in merged else sub
        return base_label, merged
    return overlay


# ---------------------------------------------------------------------------
# resolver
# ---------------------------------------------------------------------------

class _Resolver:
    def __init__(self, documents: list[ResolveDocument]):
        self.docs: dict[str, Any] = {}
        for d in documents:
            if d.document_id in self.docs:
                raise ResolveValidationError(
                    f"duplicate document_id {d.document_id!r}"
                )
            self.docs[d.document_id] = d.document
        self.dependencies: list[str] = []
        self._seen: set[str] = set()

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _label(doc_id: str, tokens: tuple[str, ...]) -> str:
        return doc_id if not tokens else f"{doc_id}#{_render(tokens)}"

    def _chain(self, stack: list[Key]) -> str:
        return " -> ".join(self._label(doc_id, tokens) for doc_id, tokens in stack)

    def _touch(self, doc_id: str) -> None:
        if doc_id not in self._seen:
            self._seen.add(doc_id)
            self.dependencies.append(doc_id)

    # -- entry point ---------------------------------------------------

    def resolve(self, root: str) -> tuple[Any, Prov]:
        doc_id, pointer = _split_reference(root)
        if not doc_id:
            raise ResolveValidationError(
                f"root must include a document_id: {root!r}"
            )
        try:
            tokens = parse_pointer(pointer if pointer is not None else "")
        except ResolveValidationError as exc:
            raise ResolveValidationError(
                f"{exc} in root reference {root!r}"
            ) from exc
        key = (doc_id, tokens)
        return self._resolve_node(key, [key])

    # -- node lookup ---------------------------------------------------

    def _lookup(self, key: Key, stack: list[Key]) -> Any:
        doc_id, tokens = key
        chain = self._chain(stack)
        if doc_id not in self.docs:
            raise ResolveValidationError(
                f"document {doc_id!r} not found (reference chain: {chain})"
            )
        self._touch(doc_id)
        node = self.docs[doc_id]
        for token in tokens:
            if isinstance(node, dict) and token in node:
                node = node[token]
                continue
            if isinstance(node, list):
                index = _array_index(token)
                if index is not None and index < len(node):
                    node = node[index]
                    continue
            raise ResolveValidationError(
                f"node not found at {self._label(*key)} "
                f"(reference chain: {chain})"
            )
        return node

    def _resolve_node(self, key: Key, stack: list[Key]) -> tuple[Any, Prov]:
        node = self._lookup(key, stack)
        return self._resolve_value(node, key[0], key[1], stack)

    # -- recursive resolution ------------------------------------------

    def _resolve_value(
        self, node: Any, doc_id: str, tokens: tuple[str, ...], stack: list[Key]
    ) -> tuple[Any, Prov]:
        if isinstance(node, dict):
            if "$ref" in node:
                return self._resolve_ref(node, doc_id, tokens, stack)
            value: dict[str, Any] = {}
            children: dict[str, Prov] = {}
            for key, sub in node.items():
                resolved, prov = self._resolve_value(
                    sub, doc_id, tokens + (key,), stack
                )
                value[key] = resolved
                children[key] = prov
            return value, (self._label(doc_id, tokens), children)
        if isinstance(node, list):
            value = []
            children = []
            for index, sub in enumerate(node):
                resolved, prov = self._resolve_value(
                    sub, doc_id, tokens + (str(index),), stack
                )
                value.append(resolved)
                children.append(prov)
            return value, (self._label(doc_id, tokens), children)
        return node, (self._label(doc_id, tokens), None)

    def _resolve_ref(
        self, node: dict, doc_id: str, tokens: tuple[str, ...], stack: list[Key]
    ) -> tuple[Any, Prov]:
        ref = node["$ref"]
        here = self._label(doc_id, tokens)
        if not isinstance(ref, str):
            raise ResolveValidationError(
                f"invalid $ref at {here}: must be a string "
                f"(reference chain: {self._chain(stack)})"
            )
        target_doc, pointer = _split_reference(ref)
        if pointer is None:
            raise ResolveValidationError(
                f"invalid reference {ref!r} at {here}: nested references must "
                f"include '#' (reference chain: {self._chain(stack)})"
            )
        if not target_doc:
            target_doc = doc_id
        try:
            target_tokens = parse_pointer(pointer)
        except ResolveValidationError as exc:
            raise ResolveValidationError(
                f"{exc} in reference {ref!r} at {here} "
                f"(reference chain: {self._chain(stack)})"
            ) from exc
        target_key = (target_doc, target_tokens)
        if target_key in stack:
            raise ResolveValidationError(
                f"circular reference detected: "
                f"{self._chain(stack + [target_key])}"
            )
        target, target_prov = self._resolve_node(target_key, stack + [target_key])

        overlay = {k: v for k, v in node.items() if k != "$ref"}
        if not overlay:
            return target, target_prov
        if not isinstance(target, dict):
            raise ResolveValidationError(
                f"cannot apply overlay keys {sorted(overlay)} to non-object "
                f"target {self._label(*target_key)} "
                f"(reference chain: {self._chain(stack + [target_key])})"
            )
        overlay_value: dict[str, Any] = {}
        overlay_children: dict[str, Prov] = {}
        for key, sub in overlay.items():
            resolved, prov = self._resolve_value(
                sub, doc_id, tokens + (key,), stack
            )
            overlay_value[key] = resolved
            overlay_children[key] = prov
        overlay_prov = (self._label(doc_id, tokens), overlay_children)
        return _deep_merge(target, overlay_value), _merge_prov(
            target_prov, overlay_prov
        )


# ---------------------------------------------------------------------------
# leaf provenance
# ---------------------------------------------------------------------------

def _collect_sources(
    value: Any, prov: Prov, tokens: tuple[str, ...], out: dict[str, str]
) -> None:
    label, children = prov
    if isinstance(children, dict) and children:
        for key, sub in children.items():
            _collect_sources(value[key], sub, tokens + (key,), out)
    elif isinstance(children, list) and children:
        for index, sub in enumerate(children):
            _collect_sources(value[index], sub, tokens + (str(index),), out)
    else:  # scalar, null, empty object or empty array
        out[_render(tokens)] = label


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def resolve_documents(
    documents: list[ResolveDocument], root: str
) -> dict[str, Any]:
    """Materialize ``root`` against the document set and report provenance."""
    resolver = _Resolver(documents)
    resolved, prov = resolver.resolve(root)
    sources: dict[str, str] = {}
    _collect_sources(resolved, prov, (), sources)
    return {
        "resolved": resolved,
        "dependencies": resolver.dependencies,
        "sources": dict(sorted(sources.items())),
    }
