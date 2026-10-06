"""End-to-end tests for incremental revision reads.

``POST /sessions/{id}/changes`` pages the committed revisions after a
caller-supplied revision cursor, returning each revision's
``/route-changes`` events relative to the previous revision.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Operation
from app.sessions import SessionRecord, SessionStore


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


def read_changes(client, session_id, after_revision, subscriptions):
    return client.post(
        f"/sessions/{session_id}/changes",
        json={
            "after_revision": after_revision,
            "subscriptions": subscriptions,
        },
    )


ALL = [{"subscriber_id": "all", "include": ["/**"]}]


# ---------------------------------------------------------------------------
# revision 0 and empty windows
# ---------------------------------------------------------------------------


def test_revision_zero_is_initial_document_and_window_starts_empty(client):
    sid = create_session(client, {"a": 1})
    resp = read_changes(client, sid, 0, ALL)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "session_id": sid,
        "current_revision": 0,
        "after_revision": 0,
        "next_cursor": 0,
        "has_more": False,
        "changes": [],
    }


def test_no_new_revisions_echoes_cursor_unchanged(client):
    sid = create_session(client, {"x": 0})
    submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/x", "set", 1)])
    first = read_changes(client, sid, 0, ALL).json()
    assert first["next_cursor"] == 1
    # polling again with the returned cursor yields the identical cursor
    second = read_changes(client, sid, first["next_cursor"], ALL).json()
    assert second["changes"] == []
    assert second["next_cursor"] == 1
    assert second["current_revision"] == 1
    assert second["has_more"] is False


# ---------------------------------------------------------------------------
# per-revision diffs versus the previous revision
# ---------------------------------------------------------------------------


def test_each_revision_is_diffed_against_the_previous_revision(client):
    sid = create_session(client, {"title": "draft", "items": ["x"]})
    submit(
        client,
        sid,
        0,
        [
            op("alice", 1, {"alice": 1}, "/title", "set", "alice-title"),
            op("alice", 2, {"alice": 2}, "/items", "insert", "A", index=0),
        ],
    )
    submit(
        client,
        sid,
        1,
        [op("bob", 1, {"bob": 1}, "/title", "set", "bob-title")],
    )
    body = read_changes(client, sid, 0, ALL).json()
    [rev1, rev2] = body["changes"]
    assert rev1["revision"] == 1
    assert rev1["routes"] == [
        {
            "subscriber_id": "all",
            "events": [
                {"type": "replace", "path": "/items",
                 "old": ["x"], "new": ["A", "x"]},
                {"type": "replace", "path": "/title",
                 "old": "draft", "new": "alice-title"},
            ],
        }
    ]
    assert rev1["unrouted"] == []
    assert rev2["revision"] == 2
    # the previous revision is rev 1, not the initial document
    assert rev2["routes"] == [
        {
            "subscriber_id": "all",
            "events": [
                {"type": "replace", "path": "/title",
                 "old": "alice-title", "new": "bob-title"}
            ],
        }
    ]
    assert body["next_cursor"] == 2
    assert body["has_more"] is False


def test_middle_cursor_returns_only_later_revisions(client):
    sid = create_session(client, {"x": 0})
    submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/x", "set", 1)])
    submit(client, sid, 1, [op("alice", 2, {"alice": 2}, "/x", "set", 2)])
    submit(client, sid, 2, [op("alice", 3, {"alice": 3}, "/x", "set", 3)])
    body = read_changes(client, sid, 1, ALL).json()
    assert [c["revision"] for c in body["changes"]] == [2, 3]
    assert body["after_revision"] == 1
    assert body["next_cursor"] == 3
    assert body["has_more"] is False


def test_committed_revision_kept_even_when_document_does_not_change(client):
    # set /x to the value it already has: a genuine new revision, but an
    # empty diff -- the revision must still be returned with empty events
    sid = create_session(client, {"x": 1})
    resp = submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/x", "set", 1)])
    assert resp.json()["revision"] == 1
    body = read_changes(client, sid, 0, ALL).json()
    assert [c["revision"] for c in body["changes"]] == [1]
    [change] = body["changes"]
    assert change["routes"] == [{"subscriber_id": "all", "events": []}]
    assert change["unrouted"] == []


# ---------------------------------------------------------------------------
# routing rules mirror POST /route-changes
# ---------------------------------------------------------------------------


def test_routing_rules_include_exclude_unrouted_and_empty_subscriber(client):
    sid = create_session(client, {"users": {"u1": 1}, "secret": 0})
    submit(
        client,
        sid,
        0,
        [
            op("alice", 1, {"alice": 1}, "/users/u1", "set", 2),
            op("alice", 2, {"alice": 2}, "/secret", "set", 1),
        ],
    )
    body = read_changes(
        client,
        sid,
        0,
        [
            {"subscriber_id": "public", "include": ["/**"], "exclude": ["/secret"]},
            {"subscriber_id": "idle", "include": ["/nothing/**"]},
        ],
    ).json()
    [change] = body["changes"]
    assert change["routes"] == [
        {"subscriber_id": "idle", "events": []},
        {
            "subscriber_id": "public",
            "events": [
                {"type": "replace", "path": "/users/u1", "old": 1, "new": 2}
            ],
        },
    ]
    assert change["unrouted"] == [
        {"type": "replace", "path": "/secret", "old": 0, "new": 1}
    ]


def test_event_can_route_to_multiple_subscribers_with_dedupe(client):
    sid = create_session(client, {})
    submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/a", "set", 1)])
    body = read_changes(
        client,
        sid,
        0,
        [
            {"subscriber_id": "s1", "include": ["/**"]},
            {"subscriber_id": "s2", "include": ["/a"]},
        ],
    ).json()
    for route in body["changes"][0]["routes"]:
        assert route["events"] == [
            {"type": "add", "path": "/a", "new": 1}
        ]
    assert body["changes"][0]["unrouted"] == []


@pytest.mark.parametrize(
    "subscriptions",
    [
        [],  # no subscribers
        [{"subscriber_id": "s", "include": ["/x/*-bad"]}],  # bad pattern
        [  # duplicate subscriber ids
            {"subscriber_id": "s", "include": ["/x"]},
            {"subscriber_id": "s", "include": ["/y"]},
        ],
    ],
)
def test_invalid_subscriptions_return_422(client, subscriptions):
    sid = create_session(client, {})
    resp = read_changes(client, sid, 0, subscriptions)
    assert resp.status_code == 422


def test_more_than_100_subscriptions_return_422(client):
    sid = create_session(client, {})
    subs = [{"subscriber_id": f"s{i}", "include": ["/**"]} for i in range(101)]
    assert read_changes(client, sid, 0, subs).status_code == 422


# ---------------------------------------------------------------------------
# paging: at most 20 revisions per response
# ---------------------------------------------------------------------------


def test_page_holds_at_most_twenty_revisions_and_reports_has_more(client):
    sid = create_session(client, {"n": 0})
    for i in range(25):
        resp = submit(
            client,
            sid,
            i,
            [op("alice", i + 1, {"alice": i + 1}, "/n", "set", i + 1)],
        )
        assert resp.status_code == 200

    first = read_changes(client, sid, 0, ALL).json()
    assert [c["revision"] for c in first["changes"]] == list(range(1, 21))
    assert first["next_cursor"] == 20
    assert first["current_revision"] == 25
    assert first["has_more"] is True

    second = read_changes(client, sid, first["next_cursor"], ALL).json()
    assert [c["revision"] for c in second["changes"]] == list(range(21, 26))
    assert second["next_cursor"] == 25
    assert second["has_more"] is False

    third = read_changes(client, sid, second["next_cursor"], ALL).json()
    assert third["changes"] == []
    assert third["next_cursor"] == 25


# ---------------------------------------------------------------------------
# late concurrent edits change the current merge, never past revision events
# ---------------------------------------------------------------------------


def test_late_concurrent_edit_changes_current_but_not_prior_revision_events(client):
    sid = create_session(client, {"title": "draft"})
    submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/title", "set", "alice")])
    # bob's concurrent write arrives a revision later and wins arbitration
    submit(client, sid, 1, [op("bob", 1, {"bob": 1}, "/title", "set", "bob")])
    state = client.get(f"/sessions/{sid}").json()
    assert state["document"] == {"title": "bob"}

    body = read_changes(client, sid, 0, ALL).json()
    [rev1, rev2] = body["changes"]
    # revision 1 is immutable: it was draft -> alice at commit time
    assert rev1["routes"] == [
        {
            "subscriber_id": "all",
            "events": [
                {"type": "replace", "path": "/title", "old": "draft", "new": "alice"}
            ],
        }
    ]
    assert rev2["routes"] == [
        {
            "subscriber_id": "all",
            "events": [
                {"type": "replace", "path": "/title", "old": "alice", "new": "bob"}
            ],
        }
    ]


# ---------------------------------------------------------------------------
# cursor validation and session lookup
# ---------------------------------------------------------------------------


def test_cursor_past_current_revision_returns_422(client):
    sid = create_session(client, {})
    resp = read_changes(client, sid, 1, ALL)
    assert resp.status_code == 422
    assert "outside the known history" in resp.json()["detail"]


def test_negative_cursor_returns_422(client):
    sid = create_session(client, {})
    resp = read_changes(client, sid, -1, ALL)
    assert resp.status_code == 422


def test_changes_unknown_session_returns_404(client):
    resp = read_changes(client, "0" * 32, 0, ALL)
    assert resp.status_code == 404


def test_changes_malformed_session_id_returns_404(client):
    resp = read_changes(client, "not-a-session-id", 0, ALL)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# retransmissions / stale / invalid batches must add no history
# ---------------------------------------------------------------------------


def test_retransmitted_batch_adds_no_history(client):
    batch = [
        op("alice", 1, {"alice": 1}, "/x", "set", 1),
        op("alice", 2, {"alice": 2}, "/y", "set", 2),
    ]
    sid = create_session(client, {})
    submit(client, sid, 0, batch)
    again = submit(client, sid, 1, batch)
    assert again.json() == {
        "session_id": sid,
        "revision": 1,
        "accepted": 0,
        "duplicates": 2,
    }
    body = read_changes(client, sid, 0, ALL).json()
    assert [c["revision"] for c in body["changes"]] == [1]
    assert body["current_revision"] == 1


def test_stale_and_invalid_batches_add_no_history(client):
    sid = create_session(client, {"x": 1})
    submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/x", "set", 2)])

    stale = submit(
        client, sid, 0, [op("bob", 1, {"bob": 1}, "/y", "set", 1)]
    )
    assert stale.status_code == 409
    invalid = submit(
        client, sid, 1, [op("alice", 3, {"alice": 3}, "/z", "set", 3)]
    )
    assert invalid.status_code == 422  # alice:2 missing

    body = read_changes(client, sid, 0, ALL).json()
    assert [c["revision"] for c in body["changes"]] == [1]
    assert body["current_revision"] == 1
    # the failed batches must not have altered the committed document
    assert body["changes"][0]["routes"] == [
        {
            "subscriber_id": "all",
            "events": [
                {"type": "replace", "path": "/x", "old": 1, "new": 2}
            ],
        }
    ]


# ---------------------------------------------------------------------------
# durability: boundaries survive a restart and cross-process visibility
# ---------------------------------------------------------------------------


def test_history_boundaries_survive_store_restart(tmp_path):
    store_dir = str(tmp_path / "sessions")
    app.state.session_store = SessionStore(store_dir)
    client = TestClient(app)
    sid = create_session(client, {"n": 0})
    submit(client, sid, 0, [op("alice", 1, {"alice": 1}, "/n", "set", 1)])
    submit(client, sid, 1, [op("alice", 2, {"alice": 2}, "/n", "set", 2)])

    app.state.session_store = SessionStore(store_dir)
    client = TestClient(app)
    body = read_changes(client, sid, 0, ALL).json()
    assert [c["revision"] for c in body["changes"]] == [1, 2]
    assert body["changes"][0]["routes"][0]["events"][0]["old"] == 0
    assert body["changes"][0]["routes"][0]["events"][0]["new"] == 1
    assert body["changes"][1]["routes"][0]["events"][0]["old"] == 1
    assert body["changes"][1]["routes"][0]["events"][0]["new"] == 2

    # and reading can continue across the restart
    submit(client, sid, 2, [op("alice", 3, {"alice": 3}, "/n", "set", 3)])
    body = read_changes(client, sid, 2, ALL).json()
    assert [c["revision"] for c in body["changes"]] == [3]


def test_committed_boundary_visible_to_second_store_instance(tmp_path):
    store_dir = str(tmp_path / "sessions")
    store_a = SessionStore(store_dir)
    store_b = SessionStore(store_dir)
    rec = store_a.create({"n": 0}, {})
    operation = Operation(
        actor="alice", seq=1, version={"alice": 1},
        path="/n", action="set", value=1,
    )
    store_a.submit_batch(rec.session_id, 0, [operation])

    from app.models import Subscription

    result = store_b.read_changes(
        rec.session_id, 0, [Subscription(subscriber_id="s", include=["/**"])]
    )
    assert result["current_revision"] == 1
    assert [c["revision"] for c in result["changes"]] == [1]
    assert result["changes"][0]["routes"][0]["events"][0]["new"] == 1


# ---------------------------------------------------------------------------
# pre-upgrade sessions: history starts at the currently visible revision
# ---------------------------------------------------------------------------


def _write_legacy_record(store, *, revision, document, version, log):
    """Persist a record in the old on-disk format (no ``history`` key)."""
    rec = SessionRecord(
        session_id=uuid.uuid4().hex,
        revision=revision,
        document=document,
        version=version,
        log=log,
        history=None,
    )
    with store._lock:
        store._persist(rec)
    return rec.session_id


def test_legacy_session_seeded_on_first_read_with_current_revision_as_origin(
    store, client
):
    sid = _write_legacy_record(
        store,
        revision=2,
        document={"x": 0},
        version={},
        log=[
            Operation.model_validate(
                op("alice", 1, {"alice": 1}, "/x", "set", 1)
            ),
            Operation.model_validate(
                op("alice", 2, {"alice": 2}, "/x", "set", 2)
            ),
        ],
    )
    # the legacy state is still readable before any changes request
    state = client.get(f"/sessions/{sid}").json()
    assert state["revision"] == 2
    assert state["document"] == {"x": 2}

    # cursor at the visible revision: window is empty
    body = read_changes(client, sid, 2, ALL).json()
    assert body["changes"] == []
    assert body["next_cursor"] == 2
    assert body["current_revision"] == 2

    # cursors before the origin (2) and past the current revision are 422
    for cursor in (0, 1, 3):
        resp = read_changes(client, sid, cursor, ALL)
        assert resp.status_code == 422, resp.text
        assert "outside the known history" in resp.json()["detail"]


def test_legacy_origin_is_persisted_and_a_new_batch_diff_against_it(store, client):
    sid = _write_legacy_record(
        store,
        revision=1,
        document={"x": 0},
        version={},
        log=[
            Operation.model_validate(
                op("alice", 1, {"alice": 1}, "/x", "set", 1)
            )
        ],
    )
    # first successful commit seeds the origin (at revision 1) and appends
    # boundary 2 in the same atomic write
    resp = submit(
        client, sid, 1, [op("alice", 2, {"alice": 2}, "/x", "set", 5)]
    )
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2

    body = read_changes(client, sid, 1, ALL).json()
    assert [c["revision"] for c in body["changes"]] == [2]
    # diffed against the currently visible legacy document {"x": 1}
    assert body["changes"][0]["routes"][0]["events"] == [
        {"type": "replace", "path": "/x", "old": 1, "new": 5}
    ]
    # cursors before the seeded origin stay invalid
    assert read_changes(client, sid, 0, ALL).status_code == 422

    # the origin itself is now durable: a brand-new store sees the same rule
    fresh = SessionStore(str(store._dir))
    raw = json.loads(fresh._path(sid).read_text(encoding="utf-8"))
    assert [b["revision"] for b in raw["history"]] == [1, 2]


def test_legacy_origin_seeded_on_read_is_visible_after_restart(store, client, tmp_path):
    sid = _write_legacy_record(
        store,
        revision=1,
        document={"x": 0},
        version={},
        log=[
            Operation.model_validate(
                op("alice", 1, {"alice": 1}, "/x", "set", 1)
            )
        ],
    )
    assert read_changes(client, sid, 1, ALL).status_code == 200
    # restart: the seeded origin must already be on disk
    app.state.session_store = SessionStore(str(store._dir))
    client = TestClient(app)
    assert read_changes(client, sid, 1, ALL).status_code == 200
    assert read_changes(client, sid, 0, ALL).status_code == 422


def test_stale_submit_on_legacy_record_does_not_seed_or_persist_origin(
    store, client
):
    sid = _write_legacy_record(
        store,
        revision=1,
        document={"x": 0},
        version={},
        log=[
            Operation.model_validate(
                op("alice", 1, {"alice": 1}, "/x", "set", 1)
            )
        ],
    )
    resp = submit(
        client, sid, 0, [op("bob", 1, {"bob": 1}, "/y", "set", 1)]
    )
    assert resp.status_code == 409
    raw = json.loads(store._path(sid).read_text(encoding="utf-8"))
    assert "history" not in raw  # failed request wrote nothing


def test_invalid_submit_on_legacy_record_does_not_seed_or_persist_origin(
    store, client
):
    sid = _write_legacy_record(
        store,
        revision=1,
        document={"x": 0},
        version={},
        log=[
            Operation.model_validate(
                op("alice", 1, {"alice": 1}, "/x", "set", 1)
            )
        ],
    )
    resp = submit(
        client, sid, 1, [op("alice", 3, {"alice": 3}, "/z", "set", 3)]
    )
    assert resp.status_code == 422
    raw = json.loads(store._path(sid).read_text(encoding="utf-8"))
    assert "history" not in raw


def test_legacy_retransmission_seeds_origin_without_adding_revision(store, client):
    dup = op("alice", 1, {"alice": 1}, "/x", "set", 1)
    sid = _write_legacy_record(
        store,
        revision=1,
        document={"x": 0},
        version={},
        log=[Operation.model_validate(dup)],
    )
    resp = submit(client, sid, 1, [dup])
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1  # no new revision
    raw = json.loads(store._path(sid).read_text(encoding="utf-8"))
    # origin seeded at the current revision, no extra boundary appended
    assert [b["revision"] for b in raw["history"]] == [1]
