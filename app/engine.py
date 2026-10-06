"""Offline CRDT-style merge engine for JSON document operation logs.

Semantics
---------
* Causality is tracked with vector clocks.  Operation ``o1`` happens-before
  ``o2`` iff ``o1.version <= o2.version`` (element-wise, and not equal);
  otherwise the two operations are concurrent.
* Logs are validated before merging: per-actor sequence numbers must be
  contiguous (starting right after the initial version), every operation
  must carry its own position in its version vector, and every causal
  reference must be satisfiable from the initial version plus the
  submitted logs.  Duplicated operations (same ``actor``/``seq``) are
  idempotently de-duplicated; the same id with a different payload is
  rejected.
* Operations are applied in a deterministic causal order (topological
  sort, ties broken by ``(actor, seq)``).  Because the greatest
  ``(actor, seq)`` among concurrent writers is applied last, concurrent
  ``set``/``delete`` on the same path are resolved deterministically.
* Deletes keep tombstones so the merge state remembers removed paths.
* Concurrent array inserts at the same position are ordered stably by
  operation id ``(actor, seq)``.
* A merge/apply request may name operations to exclude.  The full logs
  are still validated first; the explicit ids are then expanded into a
  pruning closure -- same-actor successors and cross-actor operations
  witnessing a dropped op (transitively) -- and the document, version,
  tombstones, conflicts and array-insert order are recomputed from the
  retained ops, whose own version vectors are never rewritten.  The
  retained ops are returned in causal order as a replayable log.
"""
from __future__ import annotations

import heapq
import json
from copy import deepcopy
from typing import Any

from .models import Operation

Vector = dict[str, int]

ARBITRATION_REASON = "concurrent writes; deterministic arbitration by (actor, seq)"


class LogValidationError(ValueError):
    """Raised when an operation log fails structural or causal validation."""


# ---------------------------------------------------------------------------
# vector clocks
# ---------------------------------------------------------------------------

def vc_leq(a: Vector, b: Vector) -> bool:
    keys = set(a) | set(b)
    return all(a.get(k, 0) <= b.get(k, 0) for k in keys)


def vc_compare(a: Vector, b: Vector) -> str:
    """Return ``equal`` / ``before`` / ``after`` / ``concurrent``."""
    if vc_leq(a, b):
        return "equal" if vc_leq(b, a) else "before"
    return "after" if vc_leq(b, a) else "concurrent"


# ---------------------------------------------------------------------------
# JSON Pointer helpers (RFC 6901)
# ---------------------------------------------------------------------------

def parse_pointer(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise LogValidationError(f"invalid JSON Pointer: {path!r}")
    return [tok.replace("~1", "/").replace("~0", "~") for tok in path.split("/")[1:]]


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _as_index(token: str) -> int | None:
    return int(token) if token.isdigit() else None


_MISSING = object()


# ---------------------------------------------------------------------------
# log validation
# ---------------------------------------------------------------------------

def _canonical(op: Operation) -> str:
    return json.dumps(op.model_dump(mode="json"), sort_keys=True, default=str)


def validate_and_dedupe(
    ops: list[Operation], initial_version: Vector
) -> tuple[list[Operation], int, Vector]:
    """Validate the combined log and remove exact duplicates.

    Returns ``(unique_ops, duplicate_count, merged_version)``.
    """
    for actor, v in initial_version.items():
        if v < 0:
            raise LogValidationError(f"initial version for {actor!r} is negative")

    uniq: dict[tuple[str, int], Operation] = {}
    duplicates = 0
    for op in ops:
        key = (op.actor, op.seq)
        if key in uniq:
            if _canonical(uniq[key]) != _canonical(op):
                raise LogValidationError(
                    f"operation {op.actor}:{op.seq} appears twice with different payloads"
                )
            duplicates += 1
            continue
        if op.version.get(op.actor) != op.seq:
            raise LogValidationError(
                f"operation {op.actor}:{op.seq} must carry its own position "
                f"in its version vector"
            )
        for actor, v in op.version.items():
            if v < 0:
                raise LogValidationError(
                    f"operation {op.actor}:{op.seq} has a negative version entry"
                )
        uniq[key] = op

    # per-actor contiguity, continuing from the initial version
    by_actor: dict[str, list[int]] = {}
    for op in uniq.values():
        by_actor.setdefault(op.actor, []).append(op.seq)
    merged_version = dict(initial_version)
    for actor, seqs in by_actor.items():
        base = initial_version.get(actor, 0)
        expected = list(range(base + 1, base + 1 + len(seqs)))
        if sorted(seqs) != expected:
            raise LogValidationError(
                f"actor {actor!r} log is not contiguous: expected seqs "
                f"{expected[0]}..{expected[-1]}, got {sorted(seqs)}"
            )
        merged_version[actor] = base + len(seqs)

    # causal references must be satisfiable
    for op in uniq.values():
        for actor, v in op.version.items():
            if v > merged_version.get(actor, 0):
                raise LogValidationError(
                    f"operation {op.actor}:{op.seq} depends on {actor}:{v} "
                    f"which is not present in the logs or the initial version"
                )

    # per-actor version vectors must not regress
    for actor in by_actor:
        chain = sorted(
            (o for o in uniq.values() if o.actor == actor), key=lambda o: o.seq
        )
        for prev, cur in zip(chain, chain[1:]):
            if not vc_leq(prev.version, cur.version):
                raise LogValidationError(
                    f"actor {actor!r} version vector regresses between "
                    f"seq {prev.seq} and {cur.seq}"
                )

    return list(uniq.values()), duplicates, merged_version


# ---------------------------------------------------------------------------
# deterministic causal ordering
# ---------------------------------------------------------------------------

def causal_order(ops: list[Operation]) -> list[Operation]:
    """Topologically sort operations by happens-before.

    Concurrent operations are emitted in ascending ``(actor, seq)`` order,
    which makes the whole application order deterministic and lets the
    greatest ``(actor, seq)`` among concurrent writers win.
    """
    n = len(ops)
    ancestors: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j and vc_leq(ops[i].version, ops[j].version) and not vc_leq(
                ops[j].version, ops[i].version
            ):
                ancestors[j].add(i)

    ready: list[tuple[tuple[str, int], int]] = [
        ((ops[i].actor, ops[i].seq), i) for i in range(n) if not ancestors[i]
    ]
    heapq.heapify(ready)
    remaining = [set(a) for a in ancestors]
    order: list[Operation] = []
    while ready:
        _, i = heapq.heappop(ready)
        order.append(ops[i])
        for j in range(n):
            if i in remaining[j]:
                remaining[j].discard(i)
                if not remaining[j]:
                    heapq.heappush(ready, ((ops[j].actor, ops[j].seq), j))
    if len(order) != n:
        raise LogValidationError("operation log contains a causality cycle")
    return order


# ---------------------------------------------------------------------------
# conflict detection
# ---------------------------------------------------------------------------

def _summary(op: Operation) -> dict[str, Any]:
    return {"actor": op.actor, "seq": op.seq, "action": op.action, "value": op.value}


def detect_conflicts(ops: list[Operation]) -> list[dict[str, Any]]:
    """Report concurrent set/delete operations on the same path.

    Only causally-maximal operations per path are considered: anything a
    later operation already supersedes is not a live conflict.  Concurrent
    operations with identical effects converge silently.
    """
    by_path: dict[str, list[Operation]] = {}
    for op in ops:
        if op.action in ("set", "delete"):
            by_path.setdefault(op.path, []).append(op)

    conflicts: list[dict[str, Any]] = []
    for path in sorted(by_path):
        group = by_path[path]
        maximal = [
            o
            for o in group
            if not any(
                vc_leq(o.version, other.version)
                and not vc_leq(other.version, o.version)
                for other in group
            )
        ]
        if len(maximal) < 2:
            continue
        effects = {
            (o.action, json.dumps(o.value, sort_keys=True, default=str))
            for o in maximal
        }
        if len(effects) == 1:
            continue
        maximal.sort(key=lambda o: (o.actor, o.seq))
        winner = maximal[-1]
        conflicts.append(
            {
                "path": path,
                "reason": ARBITRATION_REASON,
                "winner": _summary(winner),
                "losers": [_summary(o) for o in maximal[:-1]],
            }
        )
    return conflicts


# ---------------------------------------------------------------------------
# document application
# ---------------------------------------------------------------------------

class _Merger:
    def __init__(self, document: Any):
        self.doc = deepcopy(document)
        self.tombstones: set[str] = set()
        # (array path, base index) -> inserts already applied there
        self.inserts: dict[tuple[str, int], list[Operation]] = {}

    # -- navigation ------------------------------------------------------

    def _node(self, tokens: list[str]) -> Any:
        node = self.doc
        for tok in tokens:
            if isinstance(node, dict):
                node = node.get(tok, _MISSING)
            elif isinstance(node, list):
                idx = _as_index(tok)
                node = node[idx] if idx is not None and idx < len(node) else _MISSING
            else:
                return _MISSING
            if node is _MISSING:
                return _MISSING
        return node

    def _ensure_parent(self, tokens: list[str]) -> tuple[Any, str]:
        """Walk to the parent of the last token, creating objects as needed."""
        if not isinstance(self.doc, (dict, list)):
            self.doc = {}
        node = self.doc
        prefix = ""
        for tok in tokens[:-1]:
            prefix += "/" + _escape(tok)
            if isinstance(node, dict):
                child = node.get(tok, _MISSING)
                if child is _MISSING or not isinstance(child, (dict, list)):
                    child = {}
                    node[tok] = child
                    self.tombstones.discard(prefix)
            elif isinstance(node, list):
                idx = _as_index(tok)
                if idx is None or idx >= len(node):
                    raise LogValidationError(
                        f"cannot traverse through array index {tok!r}"
                    )
                child = node[idx]
                if not isinstance(child, (dict, list)):
                    child = {}
                    node[idx] = child
            else:  # pragma: no cover - guarded above
                raise LogValidationError("cannot traverse through a scalar")
            node = child
        return node, tokens[-1]

    # -- actions -----------------------------------------------------------

    def _set(self, tokens: list[str], value: Any) -> None:
        if not tokens:
            self.doc = deepcopy(value)
            return
        parent, last = self._ensure_parent(tokens)
        if isinstance(parent, dict):
            parent[last] = deepcopy(value)
        else:
            idx = _as_index(last)
            if idx is None or idx > len(parent):
                raise LogValidationError(f"bad array index {last!r}")
            if idx == len(parent):
                parent.append(deepcopy(value))
            else:
                parent[idx] = deepcopy(value)

    def _delete(self, tokens: list[str], path: str) -> None:
        if not tokens:
            self.doc = None
            self.tombstones.add(path)
            return
        parent = self._node(tokens[:-1])
        if parent is _MISSING:
            return  # already gone; idempotent delete
        last = tokens[-1]
        if isinstance(parent, dict):
            if last in parent:
                del parent[last]
                self.tombstones.add(path)
        elif isinstance(parent, list):
            idx = _as_index(last)
            if idx is not None and idx < len(parent):
                parent.pop(idx)
                self.tombstones.add(path)

    def _insert(self, tokens: list[str], op: Operation) -> None:
        if tokens:
            parent, last = self._ensure_parent(tokens)
            if isinstance(parent, dict):
                target = parent.get(last, _MISSING)
                if target is _MISSING:
                    target = []
                    parent[last] = target
                    self.tombstones.discard(op.path)
            else:
                idx = _as_index(last)
                if idx is None or idx >= len(parent):
                    raise LogValidationError(f"bad array index {last!r}")
                target = parent[idx]
        else:
            target = self.doc
        if not isinstance(target, list):
            raise LogValidationError(
                f"insert target {op.path!r} is not an array"
            )
        index = op.index if op.index is not None else len(target)
        index = max(0, min(index, len(target)))
        key = (op.path, index)
        prior = self.inserts.setdefault(key, [])
        # concurrent inserts at the same position are ordered by op id
        offset = sum(
            1 for p in prior if vc_compare(p.version, op.version) == "concurrent"
        )
        target.insert(min(index + offset, len(target)), deepcopy(op.value))
        prior.append(op)

    def apply(self, op: Operation) -> None:
        tokens = parse_pointer(op.path)
        if op.action == "set":
            self._set(tokens, op.value)
            self.tombstones.discard(op.path)
        elif op.action == "delete":
            self._delete(tokens, op.path)
        elif op.action == "insert":
            self._insert(tokens, op)
        else:  # pragma: no cover - guarded by the pydantic model
            raise LogValidationError(f"unknown action {op.action!r}")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def _op_label(op: Operation) -> str:
    return f"{op.actor}:{op.seq}"


def prune_operations(
    ops: list[Operation], excluded_ids: set[tuple[str, int]]
) -> tuple[set[tuple[str, int]], dict[tuple[str, int], tuple[str, str]]]:
    """Expand an explicit exclusion set into the full pruned closure.

    Besides the explicitly named operations, two kinds of operations must
    be dropped together with them:

    * every same-actor operation with a higher seq (that actor's log stays
      a contiguous prefix, so its remaining ops are replayable);
    * any cross-actor operation whose version vector witnesses a dropped
      operation -- transitively (an op depending on a cascaded-out op is
      cascaded out as well), otherwise the retained log would reference
      history it no longer contains.

    Returns ``(pruned_keys, meta)`` where ``meta`` maps every pruned key to
    ``(mode, reason)``: ``mode`` is ``"explicit"`` or ``"cascade"`` and
    ``reason`` is ``"explicitly excluded"`` or the ``actor:seq`` label of
    the operation that forced it out.
    """
    by_key = {(o.actor, o.seq): o for o in ops}
    pruned = {key for key in excluded_ids if key in by_key}
    # phase 1: expand the closure to a fixpoint
    while True:
        before = len(pruned)
        for key, op in by_key.items():
            if key in pruned:
                continue
            # same actor: everything above a dropped seq of that actor is a
            # successor of a dropped operation
            if any(a == op.actor and s < op.seq for a, s in pruned):
                pruned.add(key)
                continue
            # cross actor: the op witnesses a dropped operation in its
            # version vector -- directly, or transitively via another
            # actor's cascaded-out chain
            if any(
                a != op.actor and op.version.get(a, 0) >= s
                for a, s in pruned
            ):
                pruned.add(key)
        if len(pruned) == before:
            break

    # phase 2: attribute each cascaded op to one triggering operation,
    # deterministically and independently of submission order
    meta: dict[tuple[str, int], tuple[str, str]] = {
        key: ("explicit", "explicitly excluded")
        for key in pruned
        if key in excluded_ids
    }
    for key in sorted(pruned):
        if key in meta:
            continue
        op = by_key[key]
        same_actor = sorted(
            s for a, s in pruned if a == op.actor and s < op.seq
        )
        if same_actor:
            cause = (op.actor, same_actor[0])
        else:
            witnesses = [
                (s, a)
                for a, s in pruned
                if a != op.actor and op.version.get(a, 0) >= s
            ]
            s, a = max(witnesses)  # closest (latest-seq) parent; ties by actor
            cause = (a, s)
        meta[key] = ("cascade", _op_label(by_key[cause]))
    return pruned, meta


def recompute_version(
    retained: list[Operation], initial_version: Vector
) -> Vector:
    """Build the result vector from retained ops (their own vectors are kept)."""
    version = dict(initial_version)
    for op in retained:
        if op.seq > version.get(op.actor, 0):
            version[op.actor] = op.seq
    return version


def _run(
    document: Any,
    initial_version: Vector,
    unique_ops: list[Operation],
    duplicates: int,
    excluded_ids: set[tuple[str, int]] | None,
) -> dict[str, Any]:
    """Order, conflict-check and apply ``unique_ops``; optionally prune first."""
    if excluded_ids is not None:
        present = {(o.actor, o.seq) for o in unique_ops}
        missing = sorted(excluded_ids - present)
        if missing:
            actor, seq = missing[0]
            raise LogValidationError(
                f"excluded operation {actor}:{seq} is not present in the logs"
            )
        pruned, meta = prune_operations(unique_ops, excluded_ids)
        retained = [o for o in unique_ops if (o.actor, o.seq) not in pruned]
        excluded_report = []
        for actor, seq in sorted(pruned):
            mode, reason = meta[(actor, seq)]
            excluded_report.append(
                {"actor": actor, "seq": seq, "mode": mode, "reason": reason}
            )
        merged_version = recompute_version(retained, initial_version)
    else:
        retained = unique_ops
        merged_version = recompute_version(retained, initial_version)

    ordered = causal_order(retained)
    conflicts = detect_conflicts(retained)

    merger = _Merger(document)
    for op in ordered:
        merger.apply(op)

    result = {
        "document": merger.doc,
        "version": merged_version,
        "conflicts": conflicts,
        "tombstones": sorted(merger.tombstones),
        "applied": len(retained),
        "duplicates": duplicates,
    }
    if excluded_ids is not None:
        result["replay_log"] = ordered
        result["excluded"] = excluded_report
    return result


def merge_logs(
    document: Any,
    version: Vector | None,
    logs: list[list[Operation]],
    exclude: list[tuple[str, int]] | None = None,
) -> dict[str, Any]:
    """Validate, order and apply one or more operation logs onto a document.

    The full logs (including every operation slated for exclusion) are
    validated and de-duplicated by the ordinary rules first.  When
    ``exclude`` is given the named operations are then dropped together
    with their same-actor successors and cross-actor dependents, and the
    result is recomputed from what remains; the retained operations' own
    version vectors are never rewritten.
    """
    initial_version = dict(version or {})
    all_ops = [op for log in logs for op in log]
    unique_ops, duplicates, _ = validate_and_dedupe(all_ops, initial_version)
    excluded_ids = set(exclude) if exclude is not None else None
    return _run(document, initial_version, unique_ops, duplicates, excluded_ids)
