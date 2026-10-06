"""End-to-end tests for the merge/apply API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


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


def merge(document=None, version=None, replica_a=(), replica_b=()):
    resp = client.post(
        "/merge",
        json={
            "document": document,
            "version": version or {},
            "replica_a": list(replica_a),
            "replica_b": list(replica_b),
        },
    )
    return resp


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------

def test_merge_disjoint_writes():
    r = merge(
        document={"title": "draft"},
        replica_a=[op("alice", 1, {"alice": 1}, "/title", "set", "final")],
        replica_b=[op("bob", 1, {"bob": 1}, "/done", "set", True)],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["document"] == {"title": "final", "done": True}
    assert body["version"] == {"alice": 1, "bob": 1}
    assert body["conflicts"] == []
    assert body["applied"] == 2
    assert body["duplicates"] == 0


def test_concurrent_same_path_deterministic_winner():
    # both replicas write /title concurrently; greatest (actor, seq) wins
    r = merge(
        document={"title": "draft"},
        replica_a=[op("alice", 1, {"alice": 1}, "/title", "set", "from-alice")],
        replica_b=[op("bob", 1, {"bob": 1}, "/title", "set", "from-bob")],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["document"]["title"] == "from-bob"  # ("bob",1) > ("alice",1)
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["path"] == "/title"
    assert conflict["winner"] == {
        "actor": "bob",
        "seq": 1,
        "action": "set",
        "value": "from-bob",
    }
    assert [l["actor"] for l in conflict["losers"]] == ["alice"]


def test_concurrent_identical_writes_converge_without_conflict():
    r = merge(
        document={},
        replica_a=[op("alice", 1, {"alice": 1}, "/x", "set", 1)],
        replica_b=[op("bob", 1, {"bob": 1}, "/x", "set", 1)],
    )
    assert r.status_code == 200
    assert r.json()["document"] == {"x": 1}
    assert r.json()["conflicts"] == []


def test_causal_dependency_is_respected():
    # bob saw alice:1 before issuing bob:1 -> his write is causally later
    r = merge(
        document={"count": 0},
        replica_a=[op("alice", 1, {"alice": 1}, "/count", "set", 1)],
        replica_b=[
            op("bob", 1, {"alice": 1, "bob": 1}, "/count", "set", 2),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["document"]["count"] == 2
    assert body["conflicts"] == []  # not concurrent -> no conflict


def test_duplicates_are_deduped():
    shared = op("alice", 1, {"alice": 1}, "/x", "set", 1)
    r = merge(document={}, replica_a=[shared], replica_b=[shared])
    assert r.status_code == 200
    body = r.json()
    assert body["document"] == {"x": 1}
    assert body["applied"] == 1
    assert body["duplicates"] == 1


def test_delete_keeps_tombstone_and_wins_arbitration():
    # zoe deletes concurrently with alice's set; ("zoe",1) wins
    r = merge(
        document={"title": "draft", "keep": 1},
        replica_a=[op("alice", 1, {"alice": 1}, "/title", "set", "edited")],
        replica_b=[op("zoe", 1, {"zoe": 1}, "/title", "delete")],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["document"] == {"keep": 1}
    assert body["tombstones"] == ["/title"]
    assert body["conflicts"][0]["winner"]["action"] == "delete"


def test_set_can_beat_delete_by_arbitration():
    # bob's concurrent set outranks alice's delete
    r = merge(
        document={"title": "draft"},
        replica_a=[op("alice", 1, {"alice": 1}, "/title", "delete")],
        replica_b=[op("bob", 1, {"bob": 1}, "/title", "set", "kept")],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["document"] == {"title": "kept"}
    assert body["tombstones"] == []


def test_later_set_resurrects_tombstoned_path():
    r = merge(
        document={"a": 1},
        replica_a=[
            op("alice", 1, {"alice": 1}, "/a", "delete"),
            op("alice", 2, {"alice": 2}, "/a", "set", 42),
        ],
        replica_b=[],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["document"] == {"a": 42}
    assert body["tombstones"] == []


def test_concurrent_array_inserts_sorted_by_op_id():
    r = merge(
        document={"items": ["x"]},
        replica_a=[
            op("alice", 1, {"alice": 1}, "/items", "insert", "A", index=0)
        ],
        replica_b=[op("bob", 1, {"bob": 1}, "/items", "insert", "B", index=0)],
    )
    assert r.status_code == 200
    body = r.json()
    # stable order by op id: alice:1 before bob:1, both before "x"
    assert body["document"]["items"] == ["A", "B", "x"]
    assert body["conflicts"] == []  # inserts accumulate, no arbitration


def test_merge_is_deterministic_regardless_of_log_split():
    ops_a = [op("alice", 1, {"alice": 1}, "/x", "set", "a")]
    ops_b = [op("bob", 1, {"bob": 1}, "/x", "set", "b")]
    r1 = merge(document={}, replica_a=ops_a, replica_b=ops_b)
    r2 = merge(document={}, replica_a=ops_b, replica_b=ops_a)
    assert r1.json()["document"] == r2.json()["document"] == {"x": "b"}


def test_version_vector_merges_initial_and_logs():
    r = merge(
        document={},
        version={"carol": 2},
        replica_a=[
            op("alice", 1, {"alice": 1}, "/x", "set", 1),
            op("alice", 2, {"alice": 2}, "/y", "set", 2),
        ],
        replica_b=[op("bob", 1, {"bob": 1}, "/z", "set", 3)],
    )
    assert r.status_code == 200
    assert r.json()["version"] == {"carol": 2, "alice": 2, "bob": 1}


def test_nested_paths_and_autocreation():
    r = merge(
        document={},
        replica_a=[
            op("alice", 1, {"alice": 1}, "/user/profile/name", "set", "Ada")
        ],
        replica_b=[],
    )
    assert r.status_code == 200
    assert r.json()["document"] == {"user": {"profile": {"name": "Ada"}}}


# ---------------------------------------------------------------------------
# validation failures -> 422
# ---------------------------------------------------------------------------

def test_conflicting_duplicate_rejected():
    a = op("alice", 1, {"alice": 1}, "/x", "set", 1)
    b = op("alice", 1, {"alice": 1}, "/x", "set", 2)  # same id, other payload
    r = merge(document={}, replica_a=[a], replica_b=[b])
    assert r.status_code == 422
    assert "different payloads" in r.json()["detail"]


def test_sequence_gap_rejected():
    r = merge(
        document={},
        replica_a=[op("alice", 2, {"alice": 2}, "/x", "set", 1)],
    )
    assert r.status_code == 422
    assert "not contiguous" in r.json()["detail"]


def test_missing_causal_dependency_rejected():
    # bob claims to have seen alice:5, but no such op is supplied
    r = merge(
        document={},
        replica_b=[op("bob", 1, {"alice": 5, "bob": 1}, "/x", "set", 1)],
    )
    assert r.status_code == 422
    assert "depends on alice:5" in r.json()["detail"]


def test_own_position_required_in_version_vector():
    r = merge(
        document={},
        replica_a=[op("alice", 1, {"bob": 1}, "/x", "set", 1)],
    )
    assert r.status_code == 422
    assert "own position" in r.json()["detail"]


def test_regressing_version_vector_rejected():
    r = merge(
        document={},
        version={"bob": 3},
        replica_a=[
            op("alice", 1, {"alice": 1, "bob": 3}, "/x", "set", 1),
            op("alice", 2, {"alice": 2}, "/y", "set", 2),
        ],
    )
    assert r.status_code == 422
    assert "regresses" in r.json()["detail"]


def test_invalid_pointer_rejected():
    r = merge(
        document={},
        replica_a=[op("alice", 1, {"alice": 1}, "not-a-pointer", "set", 1)],
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /apply
# ---------------------------------------------------------------------------

def test_apply_replays_single_log():
    resp = client.post(
        "/apply",
        json={
            "document": {"count": 0},
            "version": {},
            "operations": [
                op("alice", 1, {"alice": 1}, "/count", "set", 1),
                op("alice", 2, {"alice": 2}, "/tags", "insert", "a", index=0),
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"] == {"count": 1, "tags": ["a"]}
    assert body["version"] == {"alice": 2}
    assert body["applied"] == 2


def test_apply_matches_merge_for_single_replica():
    log = [
        op("alice", 1, {"alice": 1}, "/x", "set", 1),
        op("alice", 2, {"alice": 2}, "/x", "delete"),
    ]
    r_apply = client.post(
        "/apply", json={"document": {}, "version": {}, "operations": log}
    )
    r_merge = merge(document={}, replica_a=log, replica_b=[])
    assert r_apply.json() == r_merge.json()
    assert r_apply.json()["tombstones"] == ["/x"]


def test_apply_validates_log():
    resp = client.post(
        "/apply",
        json={
            "document": {},
            "operations": [op("alice", 3, {"alice": 3}, "/x", "set", 1)],
        },
    )
    assert resp.status_code == 422


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}
