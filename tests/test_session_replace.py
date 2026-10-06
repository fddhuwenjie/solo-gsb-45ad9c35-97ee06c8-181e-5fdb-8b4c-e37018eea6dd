"""End-to-end tests for JSONPath multi-path session replacement.

``POST /sessions/{id}/replace`` evaluates one ``/query``-syntax JSONPath
against the current merged document and, in a single atomic commit, writes
one ``set`` operation per existing match, numbered contiguously for the
request's actor.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Operation
from app.sessions import RevisionConflictError, SessionStore


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


def create_session(client, document=None, version=None):
    resp = client.post(
        "/sessions", json={"document": document, "version": version or {}}
    )
    assert resp.status_code == 200
    return resp.json()["session_id"]


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
# happy path: multiple pre-ordered set operations in one commit
# ---------------------------------------------------------------------------


def test_wildcard_replace_generates_preordered_contiguous_operations(client):
    sid = create_session(client, {"users": {"a": 1, "b": 2, "c": 3}})
    resp = replace(client, sid, 0, "alice", "$.users[*]", 0)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 1
    assert body["matched_paths"] == ["/users/a", "/users/b", "/users/c"]
    assert [(o["actor"], o["seq"], o["path"]) for o in body["operations"]] == [
        ("alice", 1, "/users/a"),
        ("alice", 2, "/users/b"),
        ("alice", 3, "/users/c"),
    ]
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"users": {"a": 0, "b": 0, "c": 0}}
    assert state["revision"] == 1
    assert state["version"] == {"alice": 3}


def test_query_alias_is_accepted_for_path_field(client):
    sid = create_session(client, {"x": 1})
    resp = client.post(
        f"/sessions/{sid}/replace",
        json={"expected_revision": 0, "actor": "a", "query": "$.x", "value": 2},
    )
    assert resp.status_code == 200
    assert resp.json()["matched_paths"] == ["/x"]


def test_filter_and_recursive_descent_share_query_syntax(client):
    sid = create_session(
        client,
        {"items": [{"v": 1, "tag": "keep"}, {"v": 2}, {"v": 3, "tag": "x"}]},
    )
    resp = replace(client, sid, 0, "a", "$.items[?(@.v >= 2)].v", 99)
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_paths"] == ["/items/1/v", "/items/2/v"]
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"]["items"] == [
        {"v": 1, "tag": "keep"},
        {"v": 99},
        {"v": 99, "tag": "x"},
    ]


def test_replace_value_may_be_any_json_type(client):
    sid = create_session(client, {"a": 1, "b": 2})
    resp = replace(client, sid, 0, "a", "$[*]", {"nested": [True, None]})
    assert resp.status_code == 200
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {
        "a": {"nested": [True, None]},
        "b": {"nested": [True, None]},
    }


def test_root_match_replaces_whole_document(client):
    sid = create_session(client, {"a": {"b": 1}})
    resp = replace(client, sid, 0, "a", "$", {"new": "doc"})
    assert resp.status_code == 200
    assert resp.json()["matched_paths"] == [""]
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"new": "doc"}
    assert state["version"] == {"a": 1}


# ---------------------------------------------------------------------------
# version vectors: cover the existing log and the prior ops of this request
# ---------------------------------------------------------------------------


def test_sequences_continue_after_existing_actor_log(client):
    sid = create_session(client, {"users": {"a": 1, "b": 2}})
    submit(
        client,
        sid,
        0,
        [
            op("alice", 1, {"alice": 1}, "/marker", "set", 1),
            op("alice", 2, {"alice": 2}, "/marker", "set", 2),
        ],
    )
    resp = replace(client, sid, 1, "alice", "$.users[*]", 0)
    assert resp.status_code == 200
    body = resp.json()
    assert [o["seq"] for o in body["operations"]] == [3, 4]
    state = client.get(f"/sessions/{sid}").json()
    assert state["version"] == {"alice": 4}


def test_sequences_start_after_initial_version(client):
    sid = create_session(
        client, {"users": {"a": 1}}, version={"alice": 4, "bob": 1}
    )
    resp = replace(client, sid, 0, "alice", "$.users[*]", 0)
    assert resp.status_code == 200
    assert [o["seq"] for o in resp.json()["operations"]] == [5]
    state = client.get(f"/sessions/{sid}").json()
    assert state["version"] == {"alice": 5, "bob": 1}


def test_generated_versions_witness_every_prior_actor(client):
    sid = create_session(client, {"users": {"a": 1, "b": 2}})
    submit(client, sid, 0, [op("bob", 1, {"bob": 1}, "/else", "set", 1)])
    resp = replace(client, sid, 1, "alice", "$.users[*]", 0)
    assert resp.status_code == 200
    # the generated log is inspectable through replay exclude-free state;
    # verify causally via a follow-up op that witnesses both alice seqs
    followup = submit(
        client,
        sid,
        2,
        [op("carol", 1, {"bob": 1, "alice": 2, "carol": 1}, "/ok", "set", 1)],
    )
    assert followup.status_code == 200


def test_within_run_each_op_witnesses_its_predecessor(client):
    sid = create_session(client, {"users": {"a": 1, "b": 2}})
    resp = replace(client, sid, 0, "alice", "$.users[*]", 0)
    assert resp.status_code == 200
    store = app.state.session_store
    rec = store.get(sid)
    seqs = [(o.seq, o.version) for o in rec.log if o.actor == "alice"]
    assert seqs == [
        (1, {"alice": 1}),
        (2, {"alice": 2}),
    ]


def test_replace_runs_through_ordinary_merge_rules_with_arbitration(client):
    # bob's earlier set wins /users/a via concurrent arbitration? No: the
    # replace ops causally come after bob, so they apply last and win.
    sid = create_session(client, {"users": {"a": 1}})
    submit(client, sid, 0, [op("bob", 1, {"bob": 1}, "/users/a", "set", "bob")])
    resp = replace(client, sid, 1, "alice", "$.users[*]", "alice")
    assert resp.status_code == 200
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"users": {"a": "alice"}}
    assert state["conflicts"] == []


def test_existing_concurrent_conflicts_are_still_accounted(client):
    sid = create_session(client, {"users": {"a": 1}, "title": "draft"})
    submit(client, sid, 0, [op("bob", 1, {"bob": 1}, "/title", "set", "bob")])
    submit(
        client,
        sid,
        1,
        [op("carol", 1, {"carol": 1}, "/title", "set", "carol")],
    )
    # revision 2; the replace commit re-merges from the initial document,
    # so the bob/carol concurrent conflict is still reported
    resp = replace(client, sid, 2, "alice", "$.users[*]", 0)
    assert resp.status_code == 200
    state = client.get(f"/sessions/{sid}").json()
    assert len(state["conflicts"]) == 1
    assert state["conflicts"][0]["path"] == "/title"


# ---------------------------------------------------------------------------
# zero hits: success without a new revision
# ---------------------------------------------------------------------------


def test_zero_hits_returns_empty_and_does_not_bump_revision(client):
    sid = create_session(client, {"a": 1})
    resp = replace(client, sid, 0, "alice", "$.missing", 0)
    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": sid,
        "revision": 0,
        "matched_paths": [],
        "operations": [],
    }
    state = client.get(f"/sessions/{sid}").json()
    assert state["revision"] == 0
    assert state["document"] == {"a": 1}
    assert state["version"] == {}


def test_zero_hits_adds_no_history_boundary(client):
    sid = create_session(client, {"a": 1})
    submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 2)])
    assert replace(client, sid, 1, "bob", "$.nothing", 0).status_code == 200
    body = client.post(
        f"/sessions/{sid}/changes",
        json={
            "after_revision": 0,
            "subscriptions": [{"subscriber_id": "all", "include": ["/**"]}],
        },
    ).json()
    assert [c["revision"] for c in body["changes"]] == [1]
    assert body["current_revision"] == 1


def test_zero_hits_still_checks_revision(client):
    sid = create_session(client, {})
    submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/x", "set", 1)])
    resp = replace(client, sid, 0, "bob", "$.nope", 0)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# >20 matches and parent/child nesting -> 422, whole batch rejected
# ---------------------------------------------------------------------------


def test_more_than_twenty_matches_returns_422(client):
    sid = create_session(client, {"items": list(range(21))})
    resp = replace(client, sid, 0, "alice", "$.items[*]", 0)
    assert resp.status_code == 422
    assert "at most 20" in resp.json()["detail"]
    state = client.get(f"/sessions/{sid}").json()
    assert state["revision"] == 0
    assert state["document"] == {"items": list(range(21))}


def test_twenty_matches_is_the_inclusive_limit(client):
    sid = create_session(client, {"items": list(range(20))})
    resp = replace(client, sid, 0, "alice", "$.items[*]", 0)
    assert resp.status_code == 200
    assert len(resp.json()["matched_paths"]) == 20


def test_recursive_descent_parent_child_returns_422(client):
    sid = create_session(client, {"a": {"b": 1}})
    resp = replace(client, sid, 0, "alice", "$..*", 0)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "parent/child" in detail
    assert "/a" in detail
    assert "/a/b" in detail
    state = client.get(f"/sessions/{sid}").json()
    assert state["revision"] == 0
    assert state["document"] == {"a": {"b": 1}}


def test_root_and_anything_are_nested(client):
    sid = create_session(client, {"a": 1})
    # recursive wildcard on a one-field doc matches /a (root is not a match
    # of $..*); craft a set where the root itself is matched alongside a
    # child using $..* on the document with a filter alternative is not
    # possible, so test root nesting via an explicit union-free path: the
    # root match alone is legal, and a query selecting root plus a child is
    # expressed as recursive descent from root including the node itself:
    resp = replace(client, sid, 0, "alice", "$..a", 0)
    assert resp.status_code == 200  # only /a selected, no nesting
    # direct containment: /a and /a/b matched together
    sid2 = create_session(client, {"a": {"b": {"a": 1, "c": 2}}})
    resp2 = replace(client, sid2, 0, "alice", "$..a", 0)
    # matches /a (the object) and /a/b/a (the number): /a contains /a/b/a
    assert resp2.status_code == 422
    assert "parent/child" in resp2.json()["detail"]


def test_sibling_matches_are_allowed(client):
    sid = create_session(client, {"a": {"x": 1}, "b": {"x": 2}})
    resp = replace(client, sid, 0, "alice", "$..x", 0)
    assert resp.status_code == 200
    assert resp.json()["matched_paths"] == ["/a/x", "/b/x"]


def test_nested_match_request_leaves_no_operations(client):
    sid = create_session(client, {"a": {"b": 1}})
    assert replace(client, sid, 0, "alice", "$..*", 0).status_code == 422
    store = app.state.session_store
    rec = store.get(sid)
    assert rec.log == []
    assert rec.revision == 0


# ---------------------------------------------------------------------------
# malformed queries / bodies
# ---------------------------------------------------------------------------


def test_malformed_jsonpath_returns_422(client):
    sid = create_session(client, {})
    resp = replace(client, sid, 0, "alice", "a.b", 0)  # missing '$'
    assert resp.status_code == 422
    assert "must start with '$'" in resp.json()["detail"]


def test_query_budget_overrun_returns_422(client):
    # a document deep/wide enough to exceed the 10_000 node visit budget
    big = {"items": [{"n": i} for i in range(10001)]}
    sid = create_session(client, big)
    resp = replace(client, sid, 0, "alice", "$.items[*].n", 0)
    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["revision"] == 0


def test_empty_actor_rejected(client):
    sid = create_session(client, {"a": 1})
    resp = client.post(
        f"/sessions/{sid}/replace",
        json={"expected_revision": 0, "actor": "", "path": "$.a", "value": 0},
    )
    assert resp.status_code == 422


def test_missing_value_field_means_json_null(client):
    sid = create_session(client, {"a": 1})
    resp = client.post(
        f"/sessions/{sid}/replace",
        json={"expected_revision": 0, "actor": "a", "path": "$.a"},
    )
    assert resp.status_code == 200
    assert client.get(f"/sessions/{sid}").json()["document"] == {"a": None}


# ---------------------------------------------------------------------------
# optimistic concurrency and cross-process atomicity
# ---------------------------------------------------------------------------


def test_stale_expected_revision_returns_409(client):
    sid = create_session(client, {"a": 1, "b": 2})
    submit(client, sid, 0, [op("bob", 1, {"bob": 1}, "/a", "set", 5)])
    resp = replace(client, sid, 0, "alice", "$[*]", 0)
    assert resp.status_code == 409
    state = client.get(f"/sessions/{sid}").json()
    assert state["revision"] == 1
    assert state["document"] == {"a": 5, "b": 2}


def test_future_expected_revision_returns_409(client):
    sid = create_session(client, {"a": 1})
    assert replace(client, sid, 7, "alice", "$.a", 0).status_code == 409


def test_concurrent_replacements_same_revision_only_one_commits(tmp_path):
    store = SessionStore(str(tmp_path / "sessions"))
    rec = store.create({"items": [1, 2]}, {})

    def attempt(actor):
        try:
            return store.replace_paths(rec.session_id, 0, actor, "$.items[*]", 0)
        except RevisionConflictError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, [f"actor{i}" for i in range(16)]))

    committed = [o for o in outcomes if o is not None]
    assert len(committed) == 1
    assert committed[0].revision == 1
    assert committed[0].matched_paths == ["/items/0", "/items/1"]
    assert store.get(rec.session_id).revision == 1
    doc = store.get_state(rec.session_id)["document"]
    assert doc == {"items": [0, 0]}


def test_loser_does_not_apply_its_operations(tmp_path):
    store_dir = str(tmp_path / "sessions")
    store_a = SessionStore(store_dir)
    store_b = SessionStore(store_dir)
    rec = store_a.create({"items": [1, 2], "other": 9}, {})
    outcome = store_a.replace_paths(rec.session_id, 0, "a", "$.items[*]", 0)
    assert outcome.revision == 1
    # store_b selected paths while it still saw revision 0, but commits after
    with pytest.raises(RevisionConflictError):
        store_b.replace_paths(rec.session_id, 0, "b", "$.other", 1)
    state = SessionStore(store_dir).get_state(rec.session_id)
    assert state["revision"] == 1
    assert state["document"] == {"items": [0, 0], "other": 9}
    assert state["version"] == {"a": 2}


# ---------------------------------------------------------------------------
# history: the commit is visible via /changes, including an empty diff
# ---------------------------------------------------------------------------


def test_replace_commit_appears_in_revision_history(client):
    sid = create_session(client, {"users": {"a": 1, "b": 2}})
    resp = replace(client, sid, 0, "alice", "$.users[*]", 9)
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
    paths = sorted(e["path"] for e in change["routes"][0]["events"])
    assert paths == ["/users/a", "/users/b"]


def test_replace_with_identical_values_keeps_empty_revision(client):
    sid = create_session(client, {"a": 5, "b": 5})
    resp = replace(client, sid, 0, "alice", "$[*]", 5)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1  # operations committed; revision bumps
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
    assert change["unrouted"] == []


def test_failed_replace_adds_no_history(client):
    sid = create_session(client, {"items": list(range(21))})
    assert replace(client, sid, 0, "alice", "$.items[*]", 0).status_code == 422
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
# session lookup
# ---------------------------------------------------------------------------


def test_replace_unknown_session_returns_404(client):
    resp = replace(client, "0" * 32, 0, "alice", "$", 1)
    assert resp.status_code == 404


def test_replace_malformed_session_id_returns_404(client):
    resp = replace(client, "nope", 0, "alice", "$", 1)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# durability: the generated log survives a store restart
# ---------------------------------------------------------------------------


def test_replace_survives_restart(tmp_path):
    store_dir = str(tmp_path / "sessions")
    app.state.session_store = SessionStore(store_dir)
    client = TestClient(app)
    sid = create_session(client, {"users": {"a": 1}})
    assert replace(client, sid, 0, "alice", "$.users[*]", 2).status_code == 200

    app.state.session_store = SessionStore(store_dir)
    client = TestClient(app)
    state = client.get(f"/sessions/{sid}").json()
    assert state["revision"] == 1
    assert state["document"] == {"users": {"a": 2}}
    assert state["version"] == {"alice": 1}

    # a normal batch can continue the actor's contiguous chain after restart
    resp = submit(
        client,
        sid,
        1,
        [op("alice", 2, {"alice": 2}, "/users/a", "set", 3)],
    )
    assert resp.status_code == 200


def test_replace_after_legacy_batch_still_validates(tmp_path):
    store_dir = str(tmp_path / "sessions")
    store = SessionStore(store_dir)
    rec = store.create({"users": {"a": 1, "b": 2}}, {})
    store.submit_batch(
        rec.session_id,
        0,
        [Operation(
            actor="bob", seq=1, version={"bob": 1},
            path="/x", action="set", value=1,
        )],
    )
    outcome = store.replace_paths(rec.session_id, 1, "alice", "$.users[*]", 0)
    assert outcome.revision == 2
    # the generated ops witness bob:1 as well as their own chain
    log = { (o.actor, o.seq): o.version for o in store.get(rec.session_id).log }
    assert log[("alice", 1)] == {"bob": 1, "alice": 1}
    assert log[("alice", 2)] == {"bob": 1, "alice": 2}
