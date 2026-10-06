"""End-to-end tests for per-session protected JSON Pointer rules.

A session may pin up to 20 distinct RFC 6901 JSON Pointers at creation;
the rule is persisted with the session and never changes afterwards.
Every batch commit (``/batches``) and every JSONPath replacement commit
(``/replace``) compares each protected location between the locked
revision's document and the would-be document: a changed location rejects
the whole commit with 422 and writes no log entry, revision or history
boundary.  Sessions written before the feature existed read the empty
rule and behave exactly as before.
"""
from __future__ import annotations

import json
import multiprocessing
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Operation
from app.sessions import SessionRecord, SessionStore


@pytest.fixture()
def client(tmp_path):
    app.state.session_store = SessionStore(str(tmp_path / "sessions"))
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
    resp = client.post(
        "/sessions",
        json={
            "document": document,
            "version": version or {},
            **(
                {"protected_paths": protected_paths}
                if protected_paths is not None
                else {}
            ),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


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
# rule creation, validation and reading
# ---------------------------------------------------------------------------


def test_create_echoes_and_reads_back_protected_paths(client):
    body = create_session(
        client,
        {"a": 1, "b": 2},
        protected_paths=["/a", "/b"],
    )
    sid = body["session_id"]
    assert body["revision"] == 0
    assert body["protected_paths"] == ["/a", "/b"]
    state = client.get(f"/sessions/{sid}").json()
    assert state["protected_paths"] == ["/a", "/b"]


def test_create_without_field_reads_empty_rule(client):
    body = create_session(client, {"a": 1})
    assert body["protected_paths"] == []
    state = client.get(f"/sessions/{body['session_id']}").json()
    assert state["protected_paths"] == []


@pytest.mark.parametrize(
    "paths",
    [
        ["a"],                # no leading slash
        ["/a", "/a"],         # duplicate
        ["/~2"],              # illegal tilde escape
        ["/a~"],              # dangling tilde
        [123],                # not a string
    ],
)
def test_invalid_protected_paths_return_422(client, paths):
    resp = client.post(
        "/sessions", json={"document": {}, "protected_paths": paths}
    )
    assert resp.status_code == 422
    # a rejected create never materializes a session
    store_dir = app.state.session_store._dir
    assert not store_dir.exists() or not any(store_dir.glob("*.json"))


def test_twenty_one_protected_paths_return_422(client):
    resp = client.post(
        "/sessions",
        json={"document": {}, "protected_paths": [f"/x{i}" for i in range(21)]},
    )
    assert resp.status_code == 422


def test_twenty_protected_paths_is_the_inclusive_limit(client):
    paths = [f"/x{i}" for i in range(20)]
    resp = client.post(
        "/sessions",
        json={"document": {f"x{i}": i for i in range(20)},
              "protected_paths": paths},
    )
    assert resp.status_code == 200
    assert resp.json()["protected_paths"] == paths


def test_root_pointer_is_a_valid_protected_path(client):
    body = create_session(client, {"a": 1}, protected_paths=[""])
    assert body["protected_paths"] == [""]


def test_escaped_pointer_is_valid_and_distinct(client):
    resp = client.post(
        "/sessions",
        json={"document": {"a/b": 1}, "protected_paths": ["/a~1b"]},
    )
    assert resp.status_code == 200


def test_rule_is_immutable_after_creation(client):
    # there is no update API: the same rules come back after unrelated
    # commits, and no request field may alter them
    body = create_session(client, {"a": 1}, protected_paths=["/a"])
    sid = body["session_id"]
    submit(client, sid, 0, [op("z", 1, {"z": 1}, "/z", "set", 1)])
    state = client.get(f"/sessions/{sid}").json()
    assert state["protected_paths"] == ["/a"]
    raw = json.loads(
        (app.state.session_store._path(sid)).read_text(encoding="utf-8")
    )
    assert raw["protected_paths"] == ["/a"]


# ---------------------------------------------------------------------------
# batch commits: protected locations must not change
# ---------------------------------------------------------------------------


def test_batch_writing_unprotected_location_commits_normally(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/b", "set", 2)])
    assert resp.status_code == 200
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"a": 1, "b": 2}
    assert state["revision"] == 1


def test_batch_changing_protected_value_returns_422_and_writes_nothing(client):
    sid = create_session(
        client, {"a": 1, "b": 2}, protected_paths=["/a"]
    )["session_id"]
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 9)])
    assert resp.status_code == 422
    assert "protected" in resp.json()["detail"]
    state = client.get(f"/sessions/{sid}").json()
    assert state["revision"] == 0
    assert state["document"] == {"a": 1, "b": 2}
    assert state["version"] == {}


def test_batch_setting_protected_location_to_same_value_still_commits(client):
    # the visible document does not change, but a new operation is stored
    # and the revision/history advance exactly like the ordinary rules say
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 1)])
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 1
    assert body["accepted"] == 1
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"a": 1}
    assert state["version"] == {"alice": 1}


def test_missing_and_explicit_null_are_different_at_protected_location(client):
    # /missing is absent; creating it changes the protected location
    sid = create_session(client, {"a": None}, protected_paths=["/a", "/m"])["session_id"]
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/m", "set", None)])
    assert resp.status_code == 422
    # deleting the explicit null also changes the location
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "delete")])
    assert resp.status_code == 422
    # rewriting null with null leaves the value untouched -> commits
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", None)])
    assert resp.status_code == 200
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"a": None}


def test_boolean_and_number_are_different_at_protected_location(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", True)])
    assert resp.status_code == 422
    sid2 = create_session(client, {"a": True}, protected_paths=["/a"])["session_id"]
    resp = submit(client, sid2, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 1)])
    assert resp.status_code == 422


def test_string_and_number_are_different_at_protected_location(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", "1")])
    assert resp.status_code == 422


def test_integer_one_and_float_one_are_equal_at_protected_location(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 1.0)])
    assert resp.status_code == 200


def test_ancestor_replacement_changes_nested_protected_location(client):
    sid = create_session(
        client, {"user": {"name": "Ada", "age": 36}},
        protected_paths=["/user/name"],
    )["session_id"]
    resp = submit(
        client,
        sid,
        0,
        [op("alice", 1, {"alice": 1}, "/user", "set", {"name": "Grace", "age": 85})],
    )
    assert resp.status_code == 422
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"user": {"name": "Ada", "age": 36}}


def test_ancestor_replacement_keeping_protected_subtree_commits(client):
    # the ancestor object is wholesale replaced, but the protected
    # descendant keeps exactly its value -> allowed
    sid = create_session(
        client, {"user": {"name": "Ada", "age": 36}},
        protected_paths=["/user/name"],
    )["session_id"]
    resp = submit(
        client,
        sid,
        0,
        [op("alice", 1, {"alice": 1}, "/user", "set", {"name": "Ada", "age": 85})],
    )
    assert resp.status_code == 200
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"user": {"name": "Ada", "age": 85}}


def test_array_insert_shifting_a_protected_index_is_rejected(client):
    sid = create_session(
        client, {"items": [10, 20, 30]}, protected_paths=["/items/1"]
    )["session_id"]
    resp = submit(
        client,
        sid,
        0,
        [op("alice", 1, {"alice": 1}, "/items", "insert", 15, index=1)],
    )
    assert resp.status_code == 422
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"items": [10, 20, 30]}


def test_array_insert_after_a_protected_index_commits(client):
    sid = create_session(
        client, {"items": [10, 20, 30]}, protected_paths=["/items/1"]
    )["session_id"]
    resp = submit(
        client,
        sid,
        0,
        [op("alice", 1, {"alice": 1}, "/items", "insert", 25, index=2)],
    )
    assert resp.status_code == 200
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"items": [10, 20, 25, 30]}


def test_array_delete_shifting_a_protected_index_is_rejected(client):
    sid = create_session(
        client, {"items": [10, 20, 30]}, protected_paths=["/items/1"]
    )["session_id"]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/items/0", "delete")]
    )
    assert resp.status_code == 422
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"items": [10, 20, 30]}


def test_protected_root_rejects_any_visible_change(client):
    sid = create_session(client, {"a": 1}, protected_paths=[""])["session_id"]
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/b", "set", 2)])
    assert resp.status_code == 422
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "", "set", {"a": 2})])
    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["revision"] == 0


def test_protected_location_that_is_missing_stays_protected(client):
    # pinning a not-yet-existing location blocks its creation
    sid = create_session(client, {}, protected_paths=["/future"])["session_id"]
    resp = submit(
        client, sid, 0, [op("alice", 1, {"alice": 1}, "/future", "set", "x")]
    )
    assert resp.status_code == 422
    # unrelated writes still go through
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/other", "set", 1)])
    assert resp.status_code == 200


def test_escaped_protected_pointer_is_enforced(client):
    sid = create_session(
        client, {"a/b": 1, "x": 0}, protected_paths=["/a~1b"]
    )["session_id"]
    ok = submit(client, sid, 0, [op("a", 1, {"a": 1}, "/x", "set", 1)])
    assert ok.status_code == 200
    blocked = submit(client, sid, 1, [op("a", 2, {"a": 2}, "/a~1b", "set", 2)])
    assert blocked.status_code == 422


def test_batch_mixing_protected_and_unprotected_writes_is_rejected_entirely(client):
    sid = create_session(
        client, {"a": 1, "b": 1}, protected_paths=["/a"]
    )["session_id"]
    resp = submit(
        client,
        sid,
        0,
        [
            op("alice", 1, {"alice": 1}, "/b", "set", 2),
            op("alice", 2, {"alice": 2}, "/a", "set", 2),
        ],
    )
    assert resp.status_code == 422
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"a": 1, "b": 1}
    assert state["revision"] == 0


def test_rejected_batch_writes_no_history_boundary(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 2)])
    assert resp.status_code == 422
    body = client.post(
        f"/sessions/{sid}/changes",
        json={
            "after_revision": 0,
            "subscriptions": [{"subscriber_id": "all", "include": ["/**"]}],
        },
    ).json()
    assert body["changes"] == []
    assert body["current_revision"] == 0


# ---------------------------------------------------------------------------
# stale revisions, validation and retransmission keep their old behavior
# ---------------------------------------------------------------------------


def test_stale_revision_returning_409_takes_priority_over_protection(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    submit(client, sid, 0, [op("z", 1, {"z": 1}, "/z", "set", 1)])
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 9)])
    assert resp.status_code == 409


def test_malformed_batch_at_protected_session_still_returns_422_for_log(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    # alice:2 without alice:1 -> contiguity failure; it also would write
    # nothing protected, so the log error is what the client must see
    resp = submit(client, sid, 0, [op("alice", 2, {"alice": 2}, "/b", "set", 1)])
    assert resp.status_code == 422
    assert "not contiguous" in resp.json()["detail"]


def test_exact_retransmission_on_protected_session_keeps_old_behavior(client):
    batch = [op("alice", 1, {"alice": 1}, "/b", "set", 1)]
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    first = submit(client, sid, 0, batch)
    assert first.status_code == 200 and first.json()["revision"] == 1
    again = submit(client, sid, 1, batch)
    assert again.status_code == 200
    assert again.json() == {
        "session_id": sid,
        "revision": 1,
        "accepted": 0,
        "duplicates": 1,
    }


# ---------------------------------------------------------------------------
# JSONPath replacement commits
# ---------------------------------------------------------------------------


def test_replace_matching_unprotected_nodes_commits(client):
    sid = create_session(
        client, {"a": 1, "b": 2}, protected_paths=["/a"]
    )["session_id"]
    resp = replace(client, sid, 0, "alice", "$.b", 9)
    assert resp.status_code == 200
    assert resp.json()["matched_paths"] == ["/b"]
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"a": 1, "b": 9}


def test_replace_matching_protected_node_returns_422(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    resp = replace(client, sid, 0, "alice", "$.a", 2)
    assert resp.status_code == 422
    assert "protected" in resp.json()["detail"]
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"a": 1}
    assert state["revision"] == 0
    assert state["version"] == {}


def test_replace_setting_protected_node_to_same_value_still_bumps(client):
    sid = create_session(client, {"a": 1, "b": 1}, protected_paths=["/a"])["session_id"]
    resp = replace(client, sid, 0, "alice", "$[*]", 1)
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 1
    assert body["matched_paths"] == ["/a", "/b"]
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"a": 1, "b": 1}  # visible doc unchanged


def test_replace_array_wildcard_covering_protected_index_is_rejected(client):
    sid = create_session(
        client, {"items": [10, 20]}, protected_paths=["/items/0"]
    )["session_id"]
    resp = replace(client, sid, 0, "alice", "$.items[*]", 0)
    assert resp.status_code == 422
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"items": [10, 20]}


def test_replace_sibling_subtree_that_shifts_a_protected_index_is_rejected(client):
    # replacing the whole /items array removes the protected position
    sid = create_session(
        client, {"items": [10, 20], "keep": 0}, protected_paths=["/items/1"]
    )["session_id"]
    resp = replace(client, sid, 0, "alice", "$.items", [9])
    assert resp.status_code == 422
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"items": [10, 20], "keep": 0}


def test_replace_root_on_protected_session_is_rejected(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    resp = replace(client, sid, 0, "alice", "$", {"a": 2})
    assert resp.status_code == 422


def test_replace_zero_hits_unchanged_on_protected_session(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    resp = replace(client, sid, 0, "alice", "$.missing", 5)
    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": sid,
        "revision": 0,
        "matched_paths": [],
        "operations": [],
    }


def test_replace_stale_revision_returns_409_even_when_it_would_protect(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    submit(client, sid, 0, [op("z", 1, {"z": 1}, "/z", "set", 1)])
    resp = replace(client, sid, 0, "alice", "$.a", 9)
    assert resp.status_code == 409


def test_rejected_replace_writes_no_history(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    assert replace(client, sid, 0, "alice", "$.a", 9).status_code == 422
    body = client.post(
        f"/sessions/{sid}/changes",
        json={
            "after_revision": 0,
            "subscriptions": [{"subscriber_id": "all", "include": ["/**"]}],
        },
    ).json()
    assert body["changes"] == []
    assert body["current_revision"] == 0


def test_accepted_protection_neutral_replace_appears_in_history(client):
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]
    resp = replace(client, sid, 0, "alice", "$.a", 1)
    assert resp.status_code == 200
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


# ---------------------------------------------------------------------------
# durability: persistence, restart and processes sharing one directory
# ---------------------------------------------------------------------------


def test_rule_survives_store_restart_and_stays_enforced(tmp_path):
    store_dir = str(tmp_path / "sessions")
    app.state.session_store = SessionStore(store_dir)
    client = TestClient(app)
    sid = create_session(client, {"a": 1}, protected_paths=["/a"])["session_id"]

    app.state.session_store = SessionStore(store_dir)
    client = TestClient(app)
    assert client.get(f"/sessions/{sid}").json()["protected_paths"] == ["/a"]
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 2)])
    assert resp.status_code == 422
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/b", "set", 2)])
    assert resp.status_code == 200


def test_second_store_instance_enforces_the_same_rule(tmp_path):
    store_dir = str(tmp_path / "sessions")
    store_a = SessionStore(store_dir)
    store_b = SessionStore(store_dir)
    rec = store_a.create({"a": 1, "b": 2}, {}, protected_paths=["/a"])
    from app.sessions import ProtectedPathError

    allowed = Operation(
        actor="alice", seq=1, version={"alice": 1},
        path="/b", action="set", value=3,
    )
    assert store_b.submit_batch(rec.session_id, 0, [allowed]).revision == 1
    blocked = Operation(
        actor="bob", seq=1, version={"bob": 1},
        path="/a", action="set", value=9,
    )
    with pytest.raises(ProtectedPathError):
        store_b.submit_batch(rec.session_id, 1, [blocked])
    state = SessionStore(store_dir).get_state(rec.session_id)
    assert state["document"] == {"a": 1, "b": 3}
    assert state["protected_paths"] == ["/a"]


def _protected_worker(store_dir, session_id, queue):
    """Subprocess worker: try to change a protected location."""
    from app.sessions import ProtectedPathError

    store = SessionStore(store_dir)
    operation = Operation(
        actor="bob", seq=1, version={"bob": 1},
        path="/a", action="set", value=99,
    )
    try:
        store.submit_batch(session_id, 0, [operation])
        queue.put("committed")
    except ProtectedPathError:
        queue.put("protected-422")
    except Exception as exc:  # pragma: no cover - report unexpected errors
        queue.put(f"error:{exc!r}")


def test_rule_enforced_in_a_separate_process_sharing_the_directory(tmp_path):
    store_dir = str(tmp_path / "sessions")
    rec = SessionStore(store_dir).create({"a": 1}, {}, protected_paths=["/a"])

    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(
        target=_protected_worker, args=(store_dir, rec.session_id, queue)
    )
    proc.start()
    proc.join(timeout=60)
    assert proc.exitcode == 0
    assert queue.get(timeout=15) == "protected-422"
    state = SessionStore(store_dir).get_state(rec.session_id)
    assert state["revision"] == 0
    assert state["document"] == {"a": 1}


# ---------------------------------------------------------------------------
# sessions written before the feature existed read the empty rule
# ---------------------------------------------------------------------------


def _write_legacy_record(store, *, revision=0, document=None, version=None, log=None):
    rec = SessionRecord(
        session_id=uuid.uuid4().hex,
        revision=revision,
        document=document if document is not None else {},
        version=version or {},
        log=log or [],
        history=None,
    )
    with store._lock:
        store._persist(rec)
    return rec.session_id


def test_legacy_session_reads_empty_rule_and_commits_unrestricted(tmp_path):
    store_dir = str(tmp_path / "sessions")
    store = SessionStore(store_dir)
    app.state.session_store = store
    client = TestClient(app)
    sid = _write_legacy_record(
        store, document={"a": 1},
        log=[Operation.model_validate(op("alice", 1, {"alice": 1}, "/a", "set", 1))],
        revision=1,
    )
    state = client.get(f"/sessions/{sid}").json()
    assert state["protected_paths"] == []
    # a write that would be illegal under a /a rule commits normally
    resp = submit(client, sid, 1, [op("alice", 2, {"alice": 2}, "/a", "set", 2)])
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2
