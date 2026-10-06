"""End-to-end tests for POST /route-changes."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def route(before, after, subscriptions):
    return client.post(
        "/route-changes",
        json={
            "before": before,
            "after": after,
            "subscriptions": subscriptions,
        },
    )


def sub(subscriber_id, include=(), exclude=()):
    return {
        "subscriber_id": subscriber_id,
        "include": list(include),
        "exclude": list(exclude),
    }


def events_for(body, subscriber_id):
    return next(
        route for route in body["routes"] if route["subscriber_id"] == subscriber_id
    )["events"]


SNAPSHOT_BEFORE = {
    "users": {
        "u1": {"name": "Ada", "age": 36},
        "u2": {"name": "Lin"},
    },
    "tags": [1, 2],
}
SNAPSHOT_AFTER = {
    "users": {
        "u1": {"name": "Ada", "age": 37},
        "u3": {"name": "Zoe"},
    },
    "tags": [1, 2, 3],
}


# ---------------------------------------------------------------------------
# diff semantics
# ---------------------------------------------------------------------------

def test_objects_recurse_to_minimal_change_paths():
    r = route(SNAPSHOT_BEFORE, SNAPSHOT_AFTER, [sub("s", ["/**"])])
    assert r.status_code == 200
    events = events_for(r.json(), "s")
    assert events == [
        {"type": "replace", "path": "/tags", "old": [1, 2], "new": [1, 2, 3]},
        {"type": "replace", "path": "/users/u1/age", "old": 36, "new": 37},
        {"type": "remove", "path": "/users/u2", "old": {"name": "Lin"}},
        {"type": "add", "path": "/users/u3", "new": {"name": "Zoe"}},
    ]


def test_added_and_removed_subtree_emits_single_event():
    before = {"keep": 1, "gone": {"deep": {"x": [1, 2], "y": "z"}}}
    after = {"keep": 1, "born": {"a": {"b": [3]}}}
    r = route(before, after, [sub("s", ["/**"])])
    events = events_for(r.json(), "s")
    assert [e["type"] for e in events] == ["add", "remove"]
    add = next(e for e in events if e["type"] == "add")
    remove = next(e for e in events if e["type"] == "remove")
    assert add["path"] == "/born" and add["new"] == {"a": {"b": [3]}}
    assert remove["path"] == "/gone"
    assert remove["old"] == {"deep": {"x": [1, 2], "y": "z"}}


def test_arrays_are_compared_as_whole_values():
    # identical nested arrays produce no events
    r = route({"a": [1, {"b": [2, 3]}]}, {"a": [1, {"b": [2, 3]}]}, [sub("s", ["/**"])])
    assert events_for(r.json(), "s") == []
    # any array difference is one replace on the array path, never per-index
    r = route({"a": [1, 2, 3]}, {"a": [1, 2, 4]}, [sub("s", ["/**"])])
    events = events_for(r.json(), "s")
    assert events == [
        {"type": "replace", "path": "/a", "old": [1, 2, 3], "new": [1, 2, 4]}
    ]


def test_type_changes_emit_one_replace_at_the_path():
    # object -> scalar, array -> object, scalar -> array
    r = route(
        {"o": {"k": 1}, "a": [1], "n": 5},
        {"o": 1, "a": {"k": 1}, "n": [5]},
        [sub("s", ["/**"])],
    )
    paths = {(e["path"], e["type"]) for e in events_for(r.json(), "s")}
    assert paths == {("/o", "replace"), ("/a", "replace"), ("/n", "replace")}


def test_bool_is_not_equal_to_number():
    r = route({"v": True}, {"v": 1}, [sub("s", ["/**"])])
    (event,) = events_for(r.json(), "s")
    assert event == {"type": "replace", "path": "/v", "old": True, "new": 1}


def test_null_add_remove_and_replace():
    r = route(
        {"a": None, "b": 1},
        {"a": 2, "c": None},
        [sub("s", ["/**"])],
    )
    assert events_for(r.json(), "s") == [
        {"type": "replace", "path": "/a", "old": None, "new": 2},
        {"type": "remove", "path": "/b", "old": 1},
        {"type": "add", "path": "/c", "new": None},
    ]


def test_root_snapshot_change_uses_empty_pointer():
    r = route({"a": 1}, [1, 2], [sub("root", [""])])
    assert r.status_code == 200
    assert events_for(r.json(), "root") == [
        {"type": "replace", "path": "", "old": {"a": 1}, "new": [1, 2]}
    ]


def test_equal_snapshots_have_no_events():
    payload = {"a": [1, {"b": 2}], "c": None}
    r = route(payload, payload, [sub("s", ["/**"])])
    assert r.status_code == 200
    assert events_for(r.json(), "s") == []
    assert r.json()["unrouted"] == []


# ---------------------------------------------------------------------------
# wildcard patterns
# ---------------------------------------------------------------------------

def test_star_matches_exactly_one_segment():
    r = route(SNAPSHOT_BEFORE, SNAPSHOT_AFTER, [sub("s", ["/users/*"])])
    paths = {e["path"] for e in events_for(r.json(), "s")}
    assert paths == {"/users/u2", "/users/u3"}  # /users/u1/age is 2 levels below


def test_double_star_matches_zero_or_more_segments():
    r = route(SNAPSHOT_BEFORE, SNAPSHOT_AFTER, [sub("s", ["/users/**"])])
    paths = {e["path"] for e in events_for(r.json(), "s")}
    assert paths == {"/users/u1/age", "/users/u2", "/users/u3"}

    # zero segments: pattern ending with /** matches the prefix itself
    r = route(SNAPSHOT_BEFORE, SNAPSHOT_AFTER, [sub("s", ["/tags/**"])])
    assert {e["path"] for e in events_for(r.json(), "s")} == {"/tags"}

    # bare /** matches everything including the root
    r = route({"a": 1}, [1], [sub("s", ["/**"])])
    assert {e["path"] for e in events_for(r.json(), "s")} == {""}


def test_literal_patterns_respect_pointer_escaping():
    before = {"a/b": 1, "a~b": 2}
    after = {"a/b": 10, "a~b": 20}
    r = route(before, after, [sub("s", ["/a~1b", "/a~0b"])])
    assert events_for(r.json(), "s") == [
        {"type": "replace", "path": "/a~0b", "old": 2, "new": 20},
        {"type": "replace", "path": "/a~1b", "old": 1, "new": 10},
    ]


def test_event_paths_escape_special_key_characters():
    r = route({"a/b": 1}, {"a/b": 2}, [sub("s", ["/**"])])
    assert events_for(r.json(), "s")[0]["path"] == "/a~1b"


def test_empty_pattern_matches_root_only():
    r = route({"a": 1}, {"a": 2}, [sub("root", [""])])
    assert events_for(r.json(), "root") == []  # change is at /a, not root
    assert [e["path"] for e in r.json()["unrouted"]] == ["/a"]


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

def test_event_requires_include_hit():
    r = route(
        SNAPSHOT_BEFORE,
        SNAPSHOT_AFTER,
        [sub("empty"), sub("users", ["/users/**"]), sub("tags", ["/tags"])],
    )
    body = r.json()
    assert events_for(body, "empty") == []
    assert {e["path"] for e in events_for(body, "users")} == {
        "/users/u1/age",
        "/users/u2",
        "/users/u3",
    }
    assert {e["path"] for e in events_for(body, "tags")} == {"/tags"}


def test_exclude_takes_precedence_over_include():
    r = route(
        SNAPSHOT_BEFORE,
        SNAPSHOT_AFTER,
        [sub("s", ["/**"], ["/tags", "/users/*/age"])],
    )
    assert {e["path"] for e in events_for(r.json(), "s")} == {
        "/users/u2",
        "/users/u3",
    }


def test_event_can_route_to_multiple_subscribers():
    r = route(
        SNAPSHOT_BEFORE,
        SNAPSHOT_AFTER,
        [sub("all", ["/**"]), sub("users", ["/users/**"])],
    )
    body = r.json()
    assert "/users/u2" in {e["path"] for e in events_for(body, "all")}
    assert "/users/u2" in {e["path"] for e in events_for(body, "users")}
    assert body["unrouted"] == []  # every event had at least one subscriber


def test_unrouted_collects_events_matching_nobody():
    r = route(
        SNAPSHOT_BEFORE,
        SNAPSHOT_AFTER,
        [sub("users", ["/users/**"])],
    )
    body = r.json()
    assert [e["path"] for e in body["unrouted"]] == ["/tags"]
    # unrouted events are not also delivered
    assert events_for(body, "users") and all(
        e["path"] != "/tags" for e in events_for(body, "users")
    )


def test_multiple_matching_includes_dedupe_to_one_event():
    r = route(
        SNAPSHOT_BEFORE,
        SNAPSHOT_AFTER,
        [sub("s", ["/users/**", "/users/u2", "/**"])],
    )
    events = events_for(r.json(), "s")
    paths = [e["path"] for e in events]
    assert len(paths) == len(set(paths))


def test_routes_and_events_are_stably_sorted():
    r = route(
        SNAPSHOT_BEFORE,
        SNAPSHOT_AFTER,
        [
            sub("zoe", ["/**"]),
            sub("alice", ["/**"]),
            sub("bob", ["/**"]),
        ],
    )
    body = r.json()
    assert [route_["subscriber_id"] for route_ in body["routes"]] == [
        "alice",
        "bob",
        "zoe",
    ]
    for route_ in body["routes"]:
        keys = [(e["path"], e["type"]) for e in route_["events"]]
        assert keys == sorted(keys)
    assert [
        (e["path"], e["type"]) for e in body["unrouted"]
    ] == sorted((e["path"], e["type"]) for e in body["unrouted"])


def test_subscriber_without_events_is_still_listed():
    r = route({"a": 1}, {"a": 2}, [sub("nothing", ["/b"])])
    body = r.json()
    assert body["routes"] == [{"subscriber_id": "nothing", "events": []}]
    assert [e["path"] for e in body["unrouted"]] == ["/a"]


def test_add_remove_events_omit_irrelevant_value_fields():
    r = route({"a": 1}, {"b": 2}, [sub("s", ["/**"])])
    events = events_for(r.json(), "s")
    assert next(e for e in events if e["type"] == "add") == {
        "type": "add",
        "path": "/b",
        "new": 2,
    }
    assert next(e for e in events if e["type"] == "remove") == {
        "type": "remove",
        "path": "/a",
        "old": 1,
    }


# ---------------------------------------------------------------------------
# validation -> 422
# ---------------------------------------------------------------------------

def test_invalid_pattern_without_leading_slash_rejected():
    r = route({}, {"a": 1}, [sub("s", ["users"])])
    assert r.status_code == 422
    assert "JSON Pointer" in r.json()["detail"]


def test_partial_wildcard_segment_rejected():
    for pattern in ["/users/*-profile", "/a**", "/**b", "/*x"]:
        r = route({}, {}, [sub("s", [pattern])])
        assert r.status_code == 422, pattern
        assert "wildcard" in r.json()["detail"], pattern


def test_bad_escape_sequence_rejected():
    for pattern in ["/a~", "/a~2", "/a~x"]:
        r = route({}, {}, [sub("s", [pattern])])
        assert r.status_code == 422, pattern
        assert "escape" in r.json()["detail"], pattern


def test_duplicate_subscriber_id_rejected():
    r = route(
        {},
        {"a": 1},
        [sub("dup", ["/**"]), sub("other", []), sub("dup", [])],
    )
    assert r.status_code == 422
    assert "duplicate subscriber_id 'dup'" in r.json()["detail"]


def test_subscriber_count_bounds_enforced():
    r = route({"a": 1}, {"a": 2}, [])
    assert r.status_code == 422

    too_many = [sub(f"s{i:03d}", ["/**"]) for i in range(101)]
    r = route({"a": 1}, {"a": 2}, too_many)
    assert r.status_code == 422

    exactly_one_hundred = [sub(f"s{i:03d}", ["/**"]) for i in range(100)]
    r = route({"a": 1}, {"a": 2}, exactly_one_hundred)
    assert r.status_code == 200


def test_blank_subscriber_id_rejected():
    r = route({}, {}, [sub("", ["/**"])])
    assert r.status_code == 422


def test_non_string_pattern_rejected():
    r = client.post(
        "/route-changes",
        json={"before": {}, "after": {}, "subscriptions": [
            {"subscriber_id": "s", "include": [42]}
        ]},
    )
    assert r.status_code == 422


def test_escaped_tilde_then_star_is_not_a_wildcard_segment():
    # '~0*' unescapes to the literal token '~*', which mixes a star into a
    # non-wildcard segment and must be rejected rather than matching keys.
    r = route({}, {}, [sub("s", ["/x~0*"])])
    assert r.status_code == 422
    assert "wildcard" in r.json()["detail"]

    # a lone wildcard segment still matches keys that happen to contain '*'
    r = route({"a": 1}, {"a": 2}, [sub("star", ["/*"])])
    assert r.status_code == 200
    assert [e["path"] for e in events_for(r.json(), "star")] == ["/a"]


def test_double_star_between_literal_segments_spans_any_depth():
    r = route(
        {"a": {"deep": {"deeply": {"b": 1}}, "b": 2}},
        {"a": {"deep": {"deeply": {"b": 10}}, "b": 20}},
        [sub("s", ["/a/**/b"])],
    )
    assert {e["path"] for e in events_for(r.json(), "s")} == {
        "/a/b",
        "/a/deep/deeply/b",
    }
