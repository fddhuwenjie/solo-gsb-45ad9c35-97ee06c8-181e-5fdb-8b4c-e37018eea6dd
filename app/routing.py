"""Snapshot diffing and subscriber routing for ``POST /route-changes``.

Semantics
---------
* Two JSON snapshots are compared recursively.  Objects descend into their
  members until the smallest changed path is found; an added or removed
  subtree, a type change or a changed scalar yields exactly one
  ``add`` / ``remove`` / ``replace`` event.  Arrays are compared as whole
  values and never diffed element by element.
* Event paths are JSON Pointers (RFC 6901); ``""`` is the document root.
* Subscription patterns are JSON Pointers with two wildcard segments:
  ``*`` matches exactly one path segment and ````**`` matches zero or more
  segments.  Wildcards must occupy an entire segment (``/a*`` is invalid).
* An event is routed to a subscriber iff it matches at least one of the
  subscriber's ``include`` patterns and none of the ``exclude`` patterns.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import Subscription

EventType = str  # "add" | "remove" | "replace"


class RoutingValidationError(ValueError):
    """Raised when a subscription pattern or the subscriber set is invalid."""


# ---------------------------------------------------------------------------
# JSON Pointer helpers (RFC 6901)
# ---------------------------------------------------------------------------

def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _render(tokens: list[str]) -> str:
    return "".join("/" + _escape(t) for t in tokens)


# ---------------------------------------------------------------------------
# wildcard patterns
# ---------------------------------------------------------------------------
#
# A compiled pattern is a tuple of segment descriptors:
#   ("lit", "key")  literal segment
#   ("*", None)     exactly one segment
#   ("**", None)    zero or more segments

def compile_pattern(pattern: str) -> tuple:
    """Compile a JSON Pointer pattern with ``*`` / ``**`` wildcard segments."""
    if not isinstance(pattern, str):
        raise RoutingValidationError(f"invalid pattern {pattern!r}: must be a string")
    if pattern != "" and not pattern.startswith("/"):
        raise RoutingValidationError(
            f"invalid pattern {pattern!r}: must be a JSON Pointer "
            f"(empty string or starting with '/')"
        )
    segments: list[tuple[str, str | None]] = []
    for raw in pattern.split("/")[1:]:
        token_parts: list[str] = []
        meta_stars = 0  # '*' characters that are wildcard metacharacters
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch == "~":
                if i + 1 >= len(raw) or raw[i + 1] not in "01":
                    raise RoutingValidationError(
                        f"invalid pattern {pattern!r}: bad escape sequence in "
                        f"segment {raw!r} ('~' must be followed by '0' or '1')"
                    )
                token_parts.append("/" if raw[i + 1] == "1" else "~")
                i += 2
            else:
                if ch == "*":
                    meta_stars += 1
                token_parts.append(ch)
                i += 1
        token = "".join(token_parts)
        if meta_stars:
            if raw not in ("*", "**"):
                raise RoutingValidationError(
                    f"invalid pattern {pattern!r}: wildcard must be an entire "
                    f"segment ('*' or '**'), got {raw!r}"
                )
            segments.append((raw, None))
        else:
            segments.append(("lit", token))
    return tuple(segments)


def pattern_matches(compiled: tuple, path_tokens: list[str]) -> bool:
    """Match compiled pattern segments against concrete path tokens.

    The reachable set holds the path positions reachable after consuming
    each pattern segment, which gives ``**`` (zero or more segments)
    semantics without recursion.
    """
    reachable = {0}
    total = len(path_tokens)
    for kind, value in compiled:
        nxt: set[int] = set()
        if kind == "**":
            for pos in reachable:
                nxt.update(range(pos, total + 1))
        elif kind == "*":
            for pos in reachable:
                if pos < total:
                    nxt.add(pos + 1)
        else:
            for pos in reachable:
                if pos < total and path_tokens[pos] == value:
                    nxt.add(pos + 1)
        reachable = nxt
        if not reachable:
            return False
    return total in reachable


# ---------------------------------------------------------------------------
# snapshot diff
# ---------------------------------------------------------------------------

def _json_equal(b: Any, a: Any) -> bool:
    """Structural equality that keeps JSON booleans distinct from numbers."""
    if isinstance(b, bool) or isinstance(a, bool):
        return isinstance(b, bool) and isinstance(a, bool) and b == a
    if isinstance(b, (int, float)) and isinstance(a, (int, float)):
        return b == a
    if isinstance(b, dict) and isinstance(a, dict):
        return b.keys() == a.keys() and all(_json_equal(b[k], a[k]) for k in b)
    if isinstance(b, list) and isinstance(a, list):
        return len(b) == len(a) and all(
            _json_equal(x, y) for x, y in zip(b, a)
        )
    return type(b) is type(a) and b == a


def diff_events(before: Any, after: Any) -> list[dict[str, Any]]:
    """Compare two JSON snapshots and return minimal-path change events.

    Each internal event is ``{"type", "tokens", ...}`` where ``tokens`` is
    the unescaped segment list.  Objects descend member by member; anything
    else (scalars, type mismatches, arrays) is compared as a whole.
    """
    events: list[dict[str, Any]] = []

    def walk(tokens: list[str], b: Any, a: Any) -> None:
        if isinstance(b, dict) and isinstance(a, dict):
            for key in sorted(set(b) | set(a)):
                child = tokens + [key]
                if key not in a:
                    events.append(
                        {"type": "remove", "tokens": child, "old": deepcopy(b[key])}
                    )
                elif key not in b:
                    events.append(
                        {"type": "add", "tokens": child, "new": deepcopy(a[key])}
                    )
                else:
                    walk(child, b[key], a[key])
        elif not _json_equal(b, a):
            event: dict[str, Any] = {"type": "replace", "tokens": tokens}
            event["old"] = deepcopy(b)
            event["new"] = deepcopy(a)
            events.append(event)

    walk([], before, after)
    return events


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

def _event_key(event: dict[str, Any]) -> tuple[str, EventType]:
    return _render(event["tokens"]), event["type"]


def _serialize(event: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"type": event["type"], "path": _render(event["tokens"])}
    if "old" in event:
        out["old"] = deepcopy(event["old"])
    if "new" in event:
        out["new"] = deepcopy(event["new"])
    return out


def _dedupe(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate (path, type) events, preserving first occurrence."""
    seen: set[tuple[str, EventType]] = set()
    unique: list[dict[str, Any]] = []
    for event in events:
        key = (event["type"], _render(event["tokens"]))
        if key not in seen:
            seen.add(key)
            unique.append(event)
    return unique


def compile_subscriptions(
    subscriptions: list[Subscription],
) -> list[tuple[str, tuple, tuple]]:
    """Validate and compile subscribers: unique ids, valid patterns.

    Returns ``(subscriber_id, compiled_includes, compiled_excludes)``
    tuples in request order; the caller sorts by id when serializing.
    """
    seen_ids: set[str] = set()
    compiled: list[tuple[str, tuple, tuple]] = []
    for sub in subscriptions:
        if sub.subscriber_id in seen_ids:
            raise RoutingValidationError(
                f"duplicate subscriber_id {sub.subscriber_id!r}"
            )
        seen_ids.add(sub.subscriber_id)
        includes = tuple(compile_pattern(p) for p in sub.include)
        excludes = tuple(compile_pattern(p) for p in sub.exclude)
        compiled.append((sub.subscriber_id, includes, excludes))
    return compiled


def route_events(
    events: list[dict[str, Any]],
    compiled: list[tuple[str, tuple, tuple]],
) -> dict[str, Any]:
    """Distribute already-computed diff events among compiled subscribers."""
    routed: dict[str, list[dict[str, Any]]] = {}
    unrouted: list[dict[str, Any]] = []
    for event in events:
        tokens = event["tokens"]
        matched_any = False
        for subscriber_id, includes, excludes in compiled:
            included = any(pattern_matches(p, tokens) for p in includes)
            excluded = any(pattern_matches(p, tokens) for p in excludes)
            if included and not excluded:
                routed.setdefault(subscriber_id, []).append(event)
                matched_any = True
        if not matched_any:
            unrouted.append(event)

    routes = [
        {
            "subscriber_id": subscriber_id,
            "events": [
                _serialize(event)
                for event in sorted(
                    _dedupe(routed.get(subscriber_id, [])), key=_event_key
                )
            ],
        }
        for subscriber_id, _, _ in sorted(compiled, key=lambda item: item[0])
    ]
    return {
        "routes": routes,
        "unrouted": [
            _serialize(event)
            for event in sorted(_dedupe(unrouted), key=_event_key)
        ],
    }


def route_changes(
    before: Any, after: Any, subscriptions: list[Subscription]
) -> dict[str, Any]:
    """Diff two snapshots and distribute the events to subscribers."""
    compiled = compile_subscriptions(subscriptions)
    events = diff_events(before, after)
    return route_events(events, compiled)
