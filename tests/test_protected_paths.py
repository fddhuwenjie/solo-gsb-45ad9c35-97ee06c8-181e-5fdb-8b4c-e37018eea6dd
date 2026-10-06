"""End-to-end tests for session-protected JSON Pointer paths.

A session may pin up to 20 distinct JSON Pointer (RFC 6901) paths at
creation time.  The rules are immutable for the session's lifetime and
every later commit -- both ``POST /sessions/{id}/batches`` and
``POST /sessions/{id}/replace`` -- is compared, inside the current
revision's locked commit, against the pre/post documents: if the value at
any protected pointer changes (missing vs null and booleans vs numbers
count as changes; ancestor replacement and array insertion shifts count
as changes), the whole commit returns 422 and nothing is written.
"""
from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Operation
from app.sessions import (
    ProtectedPathError,
    RevisionConflictError,
    SessionRecord,
    SessionStore,
)


@pytest.fixture()
def store(tmp_path):
    store = SessionStore(str(tmp_path / "sessions"))
    app.state.session_store = store
    return store


@pytest.fixture()
def client(store):
    return TestClient(app)


def op(actor, seq, version, path, action, value=None, index=None):
    body = {
        "actor": actor,
        "seq": seq,
        "version": version,
        "path": path,
        "action": action,
        "value": value,
    }
    if index is not None:
        body["index"] = index
    return body


def create_session(client, document=None, version=None, protected_paths=None):
    body = {"document": document, "version": version or {}}
    if protected_paths is not None:
        body["protected_paths"] = protected_paths
    resp = client.post("/sessions", json=body)
    assert resp.status_code == 200, resp.text
    return resp


def submit(client, session_id, expected_revision, operations):
    return client.post(
        f"/sessions/{session_id}/batches",
        json={"expected_revision": expected_revision, "operations": operations},
    )


def replace(client, session_id, expected_revision, actor, path, value=None):
    return client.post(
        f"/sessions/{session_id}/replace",
        json={
            "expected_revision": expected_revision,
            "actor": actor,
            "path": path,
            "value": value,
        },
    )


# ---------------------------------------------------------------------------
# creation: validation, persistence in the response and in GET
# ---------------------------------------------------------------------------


def test_create_response_echoes_protected_paths(client):
    resp = create_session(client, {"a": 1}, protected_paths=["/a"])
    assert resp.json()["protected_paths"] == ["/a"]
    sid = resp.json()["session_id"]
    state = client.get(f"/sessions/{sid}").json()
    assert state["protected_paths"] == ["/a"]


def test_protected_paths_default_to_empty(client):
    resp = create_session(client, {"a": 1})
    assert resp.json()["protected_paths"] == []
    state = client.get(f"/sessions/{resp.json()['session_id']}").json()
    assert state["protected_paths"] == []


def test_root_pointer_is_accepted(client):
    resp = create_session(client, {"a": 1}, protected_paths=[""])
    assert resp.json()["protected_paths"] == [""]


def test_twenty_distinct_paths_are_accepted(client):
    paths = [f"/k{i}" for i in range(20)]
    resp = create_session(client, {f"k{i}": i for i in range(20)}, protected_paths=paths)
    assert resp.status_code == 200
    assert resp.json()["protected_paths"] == paths


def test_twenty_one_paths_return_422(client):
    paths = [f"/k{i}" for i in range(21)]
    resp = client.post(
        "/sessions",
        json={"document": {}, "version": {}, "protected_paths": paths},
    )
    assert resp.status_code == 422


def test_duplicate_paths_return_422(client):
    resp = client.post(
        "/sessions",
        json={
            "document": {"a": 1},
            "version": {},
            "protected_paths": ["/a", "/b", "/a"],
        },
    )
    assert resp.status_code == 422
    detail = json.dumps(resp.json()["detail"])
    assert "duplicate" in detail


@pytest.mark.parametrize(
    "bad_pointer",
    [
        "a",          # no leading slash
        "/a/~",       # dangling tilde
        "/a/~2",      # bad escape
        "x/y",        # must start with '/'
    ],
)
def test_invalid_pointer_syntax_returns_422(client, bad_pointer):
    resp = client.post(
        "/sessions",
        json={
            "document": {},
            "version": {},
            "protected_paths": [bad_pointer],
        },
    )
    assert resp.status_code == 422


def test_escaped_pointer_syntax_is_accepted(client):
    resp = create_session(
        client, {"a/b": 1, "c~d": 2}, protected_paths=["/a~1b", "/c~0d"]
    )
    assert resp.status_code == 200


def test_protected_paths_are_immutable_after_creation(client):
    # there is no update endpoint; a second create simply makes another
    # session with its own rules -- the original session keeps its rules.
    first = create_session(client, {"a": 1}, protected_paths=["/a"]).json()
    second = create_session(client, {"a": 1}, protected_paths=[]).json()
    assert client.get(f"/sessions/{first['session_id']}").json()[
        "protected_paths"
    ] == ["/a"]
    assert client.get(f"/sessions/{second['session_id']}").json()[
        "protected_paths"
    ] == []


# ---------------------------------------------------------------------------
# /batches: direct writes to protected locations
# ---------------------------------------------------------------------------


def test_batch_setting_protected_path_is_rejected(client):
    sid = create_session(client, {"a": 1, "b": 2}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 99)]
    )
    assert resp.status_code == 422
    assert "/a" in resp.json()["detail"]
    state = client.get(f"/sessions/{sid}").json()
    assert state["revision"] == 0
    assert state["document"] == {"a": 1, "b": 2}
    assert state["version"] == {}


def test_batch_deleting_protected_path_is_rejected(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "delete")]
    )
    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["document"] == {"a": 1}


def test_batch_setting_the_same_value_is_allowed(client):
    sid = create_session(client, {"a": 5}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 5)]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] == 1
    assert body["revision"] == 1
    # new operations were generated but the visible document is unchanged
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"a": 5}
    assert state["version"] == {"alice": 1}


def test_batch_touching_unprotected_paths_is_allowed(client):
    sid = create_session(client, {"a": 1, "b": 2}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/b", "set", 99)]
    )
    assert resp.status_code == 200
    assert client.get(f"/sessions/{sid}").json()["document"] == {"a": 1, "b": 99}


def test_one_changed_protected_path_rejects_the_whole_batch(client):
    sid = create_session(
        client,
        {"a": 1, "b": 2, "c": 3},
        protected_paths=["/a", "/c"],
    ).json()["session_id"]
    resp = submit(
        client,
        sid,
        0,
        [
            op("alice", 1, {"alice": 1}, "/b", "set", 20),
            op("alice", 2, {"alice": 2}, "/c", "set", 30),
        ],
    )
    assert resp.status_code == 422
    state = client.get(f"/sessions/{sid}").json()
    assert state["revision"] == 0
    assert state["document"] == {"a": 1, "b": 2, "c": 3}


# ---------------------------------------------------------------------------
# type distinctions: missing vs null, booleans vs numbers
# ---------------------------------------------------------------------------


def test_setting_missing_protected_location_to_null_is_rejected(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/missing"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/missing", "set", None)]
    )
    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["document"] == {"a": 1}


def test_deleting_null_protected_location_is_rejected(client):
    sid = create_session(client, {"a": None}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "delete")]
    )
    assert resp.status_code == 422
    state = client.get(f"/sessions/{sid}").json()
    assert "a" in state["document"]
    assert state["document"]["a"] is None


def test_setting_null_protected_location_back_to_null_is_allowed(client):
    sid = create_session(client, {"a": None}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", None)]
    )
    assert resp.status_code == 200
    assert client.get(f"/sessions/{sid}").json()["document"] == {"a": None}


def test_setting_missing_protected_location_to_a_value_is_rejected(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/x"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/x", "set", 1)]
    )
    assert resp.status_code == 422


def test_boolean_vs_number_is_a_change(client):
    sid = create_session(client, {"flag": True}, protected_paths=["/flag"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/flag", "set", 1)]
    )
    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["document"] == {"flag": True}


def test_number_vs_boolean_is_a_change(client):
    sid = create_session(client, {"flag": 0}, protected_paths=["/flag"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/flag", "set", False)]
    )
    assert resp.status_code == 422


def test_string_vs_number_is_a_change(client):
    sid = create_session(client, {"n": 1}, protected_paths=["/n"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/n", "set", "1")]
    )
    assert resp.status_code == 422


def test_int_and_float_with_same_value_are_unchanged(client):
    sid = create_session(client, {"n": 1}, protected_paths=["/n"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/n", "set", 1.0)]
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# ancestor replacement and array insertion shift protected locations
# ---------------------------------------------------------------------------


def test_replacing_protected_path_ancestor_is_rejected(client):
    sid = create_session(
        client, {"user": {"name": "Ada", "age": 36}}, protected_paths=["/user/name"]
    ).json()["session_id"]
    resp = submit(
        client,
        sid,
        0,
        [op("alice", 1, {"alice": 1}, "/user", "set", {"name": "Lin"})],
    )
    assert resp.status_code == 422
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"user": {"name": "Ada", "age": 36}}


def test_ancestor_replacement_keeping_the_same_subtree_value_is_allowed(client):
    # the whole /user subtree is overwritten with an identical subtree:
    # the protected descendant value is unchanged, so the commit stands
    sid = create_session(
        client, {"user": {"name": "Ada", "age": 36}}, protected_paths=["/user/name"]
    ).json()["session_id"]
    resp = submit(
        client,
        sid,
        0,
        [
            op(
                "alice",
                1,
                {"alice": 1},
                "/user",
                "set",
                {"name": "Ada", "age": 36},
            )
        ],
    )
    assert resp.status_code == 200
    assert client.get(f"/sessions/{sid}").json()["document"] == {
        "user": {"name": "Ada", "age": 36}
    }


def test_deleting_protected_path_ancestor_is_rejected(client):
    sid = create_session(client, {"a": {"b": 1}}, protected_paths=["/a/b"]).json()[
        "session_id"
    ]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "delete")]
    )
    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["document"] == {"a": {"b": 1}}


def test_array_insertion_shifting_a_protected_index_is_rejected(client):
    sid = create_session(client, {"items": ["x", "y"]}, protected_paths=["/items/1"]).json()[
        "session_id"
    ]
    # insert at index 0 moves 'y' away from /items/1
    resp = submit(
        client,
        sid,
        0,
        [op("alice", 1, {"alice": 1}, "/items", "insert", "A", index=0)],
    )
    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["document"] == {
        "items": ["x", "y"]
    }


def test_array_insertion_after_a_protected_index_is_allowed(client):
    sid = create_session(client, {"items": ["x", "y"]}, protected_paths=["/items/0"]).json()[
        "session_id"
    ]
    resp = submit(
        client,
        sid,
        0,
        [op("alice", 1, {"alice": 1}, "/items", "insert", "z", index=2)],
    )
    assert resp.status_code == 200
    assert client.get(f"/sessions/{sid}").json()["document"] == {
        "items": ["x", "y", "z"]
    }


def test_array_deletion_shifting_a_protected_index_is_rejected(client):
    sid = create_session(
        client, {"items": ["x", "y", "z"]}, protected_paths=["/items/2"]
    ).json()["session_id"]
    resp = submit(
        client,
        sid,
        0,
        [op("alice", 1, {"alice": 1}, "/items/0", "delete")],
    )
    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["document"] == {
        "items": ["x", "y", "z"]
    }


def test_protected_root_rejects_any_document_change(client):
    sid = create_session(client, {"a": 1}, protected_paths=[""]).json()["session_id"]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/b", "set", 2)]
    )
    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["document"] == {"a": 1}


def test_protected_root_allows_value_identical_commit(client):
    doc = {"a": 1}
    sid = create_session(client, doc, protected_paths=[""]).json()["session_id"]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 1)]
    )
    assert resp.status_code == 200
    assert client.get(f"/sessions/{sid}").json()["document"] == doc


def test_protected_index_into_object_is_missing_and_stays_missing(client):
    # /0 on an object never resolves: adding/removing unrelated members
    # must still leave it missing, hence be allowed
    sid = create_session(client, {"a": 1}, protected_paths=["/0"]).json()["session_id"]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/b", "set", 2)]
    )
    assert resp.status_code == 200


def test_escaped_protected_pointer_is_enforced(client):
    sid = create_session(
        client, {"a/b": 1, "c~d": 2}, protected_paths=["/a~1b", "/c~0d"]
    ).json()["session_id"]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a~1b", "set", 9)]
    )
    assert resp.status_code == 422
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"a/b": 1, "c~d": 2}
    ok = submit(
        client, sid, 0, [op("bob", 1, {"bob": 1}, "/other", "set", 1)]
    )
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# no history / log written on rejection; identical-value commit still bumps
# ---------------------------------------------------------------------------


def test_rejected_batch_writes_no_log_revision_or_history(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    assert submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 2)]
    ).status_code == 422
    rec = app.state.session_store.get(sid)
    assert rec.revision == 0
    assert rec.log == []
    body = client.post(
        f"/sessions/{sid}/changes",
        json={
            "after_revision": 0,
            "subscriptions": [{"subscriber_id": "all", "include": ["/**"]}],
        },
    ).json()
    assert body["changes"] == []
    assert body["current_revision"] == 0


def test_identical_value_commit_keeps_empty_event_revision(client):
    sid = create_session(client, {"a": 5}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    assert submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 5)]
    ).status_code == 200
    body = client.post(
        f"/sessions/{sid}/changes",
        json={
            "after_revision": 0,
            "subscriptions": [{"subscriber_id": "all", "include": ["/**"]}],
        },
    ).json()
    [change] = body["changes"]
    assert change["revision"] == 1
    assert change["routes"] == [{"subscriber_id": "all", "events": []}]


def test_rules_still_enforced_after_an_earlier_accepted_commit(client):
    sid = create_session(client, {"a": 1, "b": 1}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    assert submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/b", "set", 2)]
    ).status_code == 200
    resp = submit(
        client, sid, 1, [op("bob", 1, {"bob": 1}, "/a", "set", 3)]
    )
    assert resp.status_code == 422
    state = client.get(f"/sessions/{sid}").json()
    assert state["revision"] == 1
    assert state["document"] == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# retransmission and stale revisions keep their original behavior
# ---------------------------------------------------------------------------


def test_retransmission_of_protected_touching_ops_still_dedupes(client):
    sid = create_session(client, {"a": 5}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    batch = [op("alice", 1, {"alice": 1}, "/a", "set", 5)]
    first = submit(client, sid, 0, batch)
    assert first.status_code == 200
    # exact retransmission at the current revision: no new ops, no 422
    second = submit(client, sid, 1, batch)
    assert second.status_code == 200
    assert second.json() == {
        "session_id": sid,
        "revision": 1,
        "accepted": 0,
        "duplicates": 1,
    }


def test_stale_revision_still_returns_409_before_protection_check(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/b", "set", 1)])
    resp = submit(
        client, sid, 0, [op("bob", 1, {"bob": 1}, "/a", "set", 9)]
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /sessions/{id}/replace
# ---------------------------------------------------------------------------


def test_replace_hitting_protected_path_is_rejected(client):
    sid = create_session(
        client, {"users": {"a": 1, "b": 2}}, protected_paths=["/users/a"]
    ).json()["session_id"]
    resp = replace(client, sid, 0, "alice", "$.users[*]", 0)
    assert resp.status_code == 422
    assert "/users/a" in resp.json()["detail"]
    state = client.get(f"/sessions/{sid}").json()
    assert state["revision"] == 0
    assert state["document"] == {"users": {"a": 1, "b": 2}}
    assert state["version"] == {}


def test_replace_elsewhere_is_allowed_when_protected_paths_unaffected(client):
    sid = create_session(
        client, {"users": {"a": 1, "b": 2}}, protected_paths=["/users/a"]
    ).json()["session_id"]
    resp = replace(client, sid, 0, "alice", "$.users.b", 9)
    assert resp.status_code == 200
    assert client.get(f"/sessions/{sid}").json()["document"] == {
        "users": {"a": 1, "b": 9}
    }


def test_replace_writing_identical_values_at_protected_paths_is_allowed(client):
    sid = create_session(
        client, {"users": {"a": 1, "b": 2}}, protected_paths=["/users/a"]
    ).json()["session_id"]
    # /users/a gets a set to its current value -- an operation exists but
    # the visible document is unchanged, so the commit proceeds.
    resp = replace(client, sid, 0, "alice", "$.users[*]", 1)
    assert resp.status_code == 200
    assert resp.json()["matched_paths"] == ["/users/a", "/users/b"]
    assert resp.json()["revision"] == 1
    assert client.get(f"/sessions/{sid}").json()["document"] == {
        "users": {"a": 1, "b": 1}
    }


def test_replace_root_protected_rejects_any_change(client):
    sid = create_session(client, {"a": 1}, protected_paths=[""]).json()["session_id"]
    resp = replace(client, sid, 0, "alice", "$.a", 2)
    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["document"] == {"a": 1}


def test_replace_array_insert_via_ancestor_set_shifts_protected_index(client):
    # a generated set that replaces a whole array ancestor moves the value
    # behind a protected index -> rejected
    sid = create_session(
        client, {"items": [1, 2]}, protected_paths=["/items/1"]
    ).json()["session_id"]
    resp = replace(client, sid, 0, "alice", "$.items", [9, 1, 2])
    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["document"] == {"items": [1, 2]}


def test_rejected_replace_writes_no_history(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"]).json()["session_id"]
    assert replace(client, sid, 0, "alice", "$.a", 2).status_code == 422
    body = client.post(
        f"/sessions/{sid}/changes",
        json={
            "after_revision": 0,
            "subscriptions": [{"subscriber_id": "all", "include": ["/**"]}],
        },
    ).json()
    assert body["changes"] == []
    assert body["current_revision"] == 0


def test_replace_zero_hits_is_unaffected_by_protection(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"]).json()["session_id"]
    resp = replace(client, sid, 0, "alice", "$.missing", 1)
    assert resp.status_code == 200
    assert resp.json()["matched_paths"] == []
    assert resp.json()["revision"] == 0


def test_replace_stale_revision_still_returns_409(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"]).json()["session_id"]
    submit(client, sid, 0, [op("bob", 1, {"bob": 1}, "/b", "set", 1)])
    assert replace(client, sid, 0, "alice", "$.a", 2).status_code == 409


# ---------------------------------------------------------------------------
# persistence: durable on disk, enforced across processes and after restart
# ---------------------------------------------------------------------------


def test_rules_survive_restart(store, client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    app.state.session_store = SessionStore(str(store._dir))
    client = TestClient(app)
    state = client.get(f"/sessions/{sid}").json()
    assert state["protected_paths"] == ["/a"]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 2)]
    )
    assert resp.status_code == 422


def test_rules_persisted_in_session_file(store, client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"]).json()[
        "session_id"
    ]
    raw = json.loads((store._dir / f"{sid}.json").read_text())
    assert raw["protected_paths"] == ["/a"]


def test_enforcement_holds_between_processes_sharing_a_directory(tmp_path):
    store_dir = str(tmp_path / "sessions")
    store_a = SessionStore(store_dir)
    rec = store_a.create({"a": 1}, {}, protected_paths=["/a"])

    def attempt(store):
        try:
            return store.submit_batch(
                rec.session_id,
                0,
                [Operation(
                    actor="bob", seq=1, version={"bob": 1},
                    path="/a", action="set", value=2,
                )],
            )
        except (RevisionConflictError, ProtectedPathError) as exc:
            return type(exc).__name__

    stores = [SessionStore(store_dir) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, stores))
    # every attempt must be refused; none may slip through as a commit.
    assert all(o != "BatchOutcome" for o in outcomes)
    fresh = SessionStore(store_dir)
    assert fresh.get(rec.session_id).revision == 0
    assert fresh.get_state(rec.session_id)["document"] == {"a": 1}


def test_legacy_session_file_without_rules_reads_as_empty(tmp_path):
    store_dir = str(tmp_path / "sessions")
    store = SessionStore(store_dir)
    sid = uuid.uuid4().hex
    store._dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "session_id": sid,
        "revision": 0,
        "document": {"a": 1},
        "version": {},
        "log": [],
        "history": [{"revision": 0, "document": {"a": 1}}],
        # no "protected_paths" key: an old client wrote this file
    }
    (store._dir / f"{sid}.json").write_text(json.dumps(legacy))
    app.state.session_store = store
    client = TestClient(app)
    state = client.get(f"/sessions/{sid}").json()
    assert state["protected_paths"] == []
    # empty rules: nothing is protected, ordinary commits go through
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 2)]
    )
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1


def test_legacy_record_with_history_and_commits_still_unprotected(tmp_path):
    store_dir = str(tmp_path / "sessions")
    store = SessionStore(store_dir)
    app.state.session_store = store
    client = TestClient(app)
    rec = SessionRecord(
        session_id=uuid.uuid4().hex,
        revision=1,
        document={"a": 1},
        version={},
        log=[Operation(
            actor="alice", seq=1, version={"alice": 1},
            path="/a", action="set", value=2,
        )],
        history=None,
        # protected_paths defaults to [] in the dataclass but we persist the
        # old on-disk format explicitly below
    )
    data = rec.to_json()
    del data["protected_paths"]
    store._dir.mkdir(parents=True, exist_ok=True)
    with store._lock:
        (store._dir / f"{rec.session_id}.json").write_text(json.dumps(data))
    state = client.get(f"/sessions/{rec.session_id}").json()
    assert state["protected_paths"] == []
    assert state["document"] == {"a": 2}
