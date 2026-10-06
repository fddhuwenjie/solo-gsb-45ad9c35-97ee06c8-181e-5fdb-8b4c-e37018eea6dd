"""End-to-end tests for operation exclusion on POST /merge and POST /apply.

Pruning requests optionally drop operations before the merge: explicitly
named ids, same-actor successors, and cross-actor operations depending on
them are removed; the retained log is recomputed and returned in causal
order so POST /apply can replay it from the same initial document.
"""
from __future__ import annotations

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


BASE_KEYS = {
    "document",
    "version",
    "conflicts",
    "tombstones",
    "applied",
    "duplicates",
}


def prune(document=None, version=None, replica_a=(), replica_b=(), exclude=(),
          field="exclude"):
    return client.post(
        "/merge",
        json={
            "document": document,
            "version": version or {},
            "replica_a": list(replica_a),
            "replica_b": list(replica_b),
            field: [{"actor": a, "seq": s} for a, s in exclude],
        },
    )


def prune_apply(document=None, version=None, operations=(), exclude=(),
                field="exclude"):
    return client.post(
        "/apply",
        json={
            "document": document,
            "version": version or {},
            "operations": list(operations),
            field: [{"actor": a, "seq": s} for a, s in exclude],
        },
    )


def replay(document, version, replay_log):
    return client.post(
        "/apply",
        json={"document": document, "version": version or {},
              "operations": replay_log},
    )


# ---------------------------------------------------------------------------
# same-actor pruning
# ---------------------------------------------------------------------------

def test_explicit_exclusion_removes_op_and_successors():
    r = prune(
        document={"a": 0},
        replica_a=[
            op("alice", 1, {"alice": 1}, "/a", "set", 1),
            op("alice", 2, {"alice": 2}, "/b", "set", 2),
            op("alice", 3, {"alice": 3}, "/c", "set", 3),
        ],
        exclude=[("alice", 2)],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["document"] == {"a": 1}
    assert body["version"] == {"alice": 1}
    assert body["applied"] == 1
    assert [(o["actor"], o["seq"]) for o in body["replay_log"]] == [
        ("alice", 1)
    ]
    assert body["excluded"] == [
        {"actor": "alice", "seq": 2, "mode": "explicit",
         "reason": "explicitly excluded"},
        {"actor": "alice", "seq": 3, "mode": "cascade",
         "reason": "alice:2"},
    ]


def test_exclude_first_op_in_chain():
    r = prune(
        document={"a": 0},
        replica_a=[
            op("alice", 1, {"alice": 1}, "/a", "set", 1),
            op("alice", 2, {"alice": 2}, "/a", "set", 2),
        ],
        exclude=[("alice", 1)],
    )
    body = r.json()
    assert body["document"] == {"a": 0}
    assert body["version"] == {}
    assert body["replay_log"] == []
    assert [e["mode"] for e in body["excluded"]] == ["explicit", "cascade"]


def test_excluding_higher_seq_keeps_lower_one():
    r = prune(
        document={},
        replica_a=[
            op("alice", 1, {"alice": 1}, "/x", "set", 1),
            op("alice", 2, {"alice": 2}, "/x", "set", 2),
        ],
        exclude=[("alice", 2)],
    )
    body = r.json()
    assert body["document"] == {"x": 1}
    assert body["replay_log"][0]["seq"] == 1


# ---------------------------------------------------------------------------
# cross-actor dependencies
# ---------------------------------------------------------------------------

def test_cross_actor_dependent_is_cascaded_out():
    r = prune(
        document={"title": "draft"},
        replica_a=[op("alice", 1, {"alice": 1}, "/title", "set", "alice")],
        replica_b=[
            op("bob", 1, {"alice": 1, "bob": 1}, "/title", "set", "bob")
        ],
        exclude=[("alice", 1)],
    )
    body = r.json()
    assert body["document"] == {"title": "draft"}
    assert body["version"] == {}
    assert body["replay_log"] == []
    assert body["excluded"] == [
        {"actor": "alice", "seq": 1, "mode": "explicit",
         "reason": "explicitly excluded"},
        {"actor": "bob", "seq": 1, "mode": "cascade",
         "reason": "alice:1"},
    ]


def test_independent_concurrent_editor_is_kept():
    r = prune(
        document={"title": "draft", "done": False},
        replica_a=[op("alice", 1, {"alice": 1}, "/title", "set", "a")],
        replica_b=[op("carol", 1, {"carol": 1}, "/done", "set", True)],
        exclude=[("alice", 1)],
    )
    body = r.json()
    assert body["document"] == {"title": "draft", "done": True}
    assert body["version"] == {"carol": 1}
    assert [(o["actor"], o["seq"]) for o in body["replay_log"]] == [
        ("carol", 1)
    ]
    assert [(e["actor"], e["seq"]) for e in body["excluded"]] == [
        ("alice", 1)
    ]


def test_concurrent_same_path_writer_that_did_not_see_excluded_is_kept():
    # bob:1 is concurrent with alice:1 (only carries bob); dropping alice
    # leaves bob as the sole writer -> no live conflict remains
    r = prune(
        document={"t": "d"},
        replica_a=[op("alice", 1, {"alice": 1}, "/t", "set", "a")],
        replica_b=[op("bob", 1, {"bob": 1}, "/t", "set", "b")],
        exclude=[("alice", 1)],
    )
    body = r.json()
    assert body["document"] == {"t": "b"}
    assert body["conflicts"] == []
    assert len(body["excluded"]) == 1


def test_transitive_cross_actor_chain_is_fully_cascaded():
    r = prune(
        document={},
        replica_a=[op("alice", 1, {"alice": 1}, "/a", "set", 1)],
        replica_b=[
            op("bob", 1, {"alice": 1, "bob": 1}, "/b", "set", 1),
            op("carol", 1, {"alice": 1, "bob": 1, "carol": 1}, "/c", "set", 1),
        ],
        exclude=[("alice", 1)],
    )
    body = r.json()
    assert body["applied"] == 0
    assert body["document"] == {}
    reasons = {(e["actor"], e["seq"]): e["reason"] for e in body["excluded"]}
    assert reasons["alice", 1] == "explicitly excluded"
    assert reasons["bob", 1] == "alice:1"
    assert reasons["carol", 1] == "bob:1"  # closest parent, not the root


def test_retained_ops_keep_original_version_vectors():
    r = prune(
        document={},
        replica_a=[
            op("alice", 1, {"alice": 1}, "/a", "set", 1),
            op("alice", 2, {"alice": 2}, "/a2", "set", 1),
        ],
        replica_b=[
            op("bob", 1, {"alice": 2, "bob": 1}, "/b", "set", 1),
            op("carol", 1, {"carol": 1}, "/c", "set", 1),
        ],
        exclude=[("alice", 2)],
    )
    body = r.json()
    # carol never saw alice, so its vector is untouched; bob is cascaded
    vectors = {o["actor"]: o["version"] for o in body["replay_log"]}
    assert vectors["carol"] == {"carol": 1}
    assert vectors["alice"] == {"alice": 1}
    assert "bob" not in vectors
    assert body["version"] == {"alice": 1, "carol": 1}


# ---------------------------------------------------------------------------
# recomputation
# ---------------------------------------------------------------------------

def test_conflicts_recomputed_after_pruning():
    # alice vs bob concurrent on /t, plus zoe setting /t after seeing both;
    # dropping alice removes the conflict she was part of
    r = prune(
        document={"t": "d"},
        replica_a=[
            op("alice", 1, {"alice": 1}, "/t", "set", "a"),
            op("zoe", 1, {"alice": 1, "bob": 1, "zoe": 1}, "/t", "set", "z"),
        ],
        replica_b=[op("bob", 1, {"bob": 1}, "/t", "set", "b")],
        exclude=[("alice", 1)],
    )
    body = r.json()
    assert body["conflicts"] == []  # zoe cascaded out; bob alone remains
    assert body["document"] == {"t": "b"}


def test_tombstones_recomputed_after_pruning():
    r = prune(
        document={"t": 0, "keep": 1},
        replica_a=[op("alice", 1, {"alice": 1}, "/t", "delete")],
        replica_b=[op("bob", 1, {"bob": 1}, "/keep", "set", 2)],
        exclude=[("alice", 1)],
    )
    body = r.json()
    assert body["tombstones"] == []
    assert body["document"] == {"t": 0, "keep": 2}


def test_array_insert_order_recomputed_after_pruning():
    r = prune(
        document={"items": ["x"]},
        replica_a=[
            op("alice", 1, {"alice": 1}, "/items", "insert", "A", index=0),
            op("alice", 2, {"alice": 2}, "/items", "insert", "A2", index=0),
        ],
        replica_b=[
            op("bob", 1, {"bob": 1}, "/items", "insert", "B", index=0)
        ],
        exclude=[("alice", 1)],
    )
    body = r.json()
    # alice:2 cascades out (same actor); only bob's insert remains
    assert body["document"]["items"] == ["B", "x"]


def test_all_ops_pruned_returns_initial_document_and_version():
    r = prune(
        document={"a": 1},
        version={"carol": 3},
        replica_a=[
            op("alice", 1, {"alice": 1, "carol": 3}, "/x", "set", 1),
            op("alice", 2, {"alice": 2, "carol": 3}, "/y", "set", 2),
        ],
        replica_b=[
            op("bob", 1, {"alice": 1, "bob": 1, "carol": 3}, "/z", "set", 3)
        ],
        exclude=[("alice", 1)],
    )
    body = r.json()
    assert body["document"] == {"a": 1}
    assert body["version"] == {"carol": 3}
    assert body["tombstones"] == []
    assert body["conflicts"] == []
    assert body["applied"] == 0
    assert body["replay_log"] == []
    assert len(body["excluded"]) == 3


# ---------------------------------------------------------------------------
# replay equivalence
# ---------------------------------------------------------------------------

def test_replay_log_reproduces_pruned_document_and_version():
    document = {"items": ["x"], "t": "d", "keep": 1}
    kwargs = dict(
        document=document,
        replica_a=[
            op("alice", 1, {"alice": 1}, "/items", "insert", "A", index=0),
            op("alice", 2, {"alice": 2}, "/t", "delete"),
        ],
        replica_b=[
            op("bob", 1, {"bob": 1}, "/items", "insert", "B", index=0),
            op("zoe", 1, {"zoe": 1}, "/t", "set", "z"),
        ],
    )
    full = client.post("/merge", json={
        "document": document, "version": {},
        "replica_a": kwargs["replica_a"], "replica_b": kwargs["replica_b"],
    }).json()
    pruned = prune(exclude=[("alice", 1)], **kwargs).json()

    # replay log is causal order starting from the SAME initial document
    replayed = replay(document, {}, pruned["replay_log"]).json()
    assert replayed["document"] == pruned["document"]
    assert replayed["version"] == pruned["version"]
    # the pruned result is a genuinely different state from the full merge
    assert pruned["document"] != full["document"]


def test_replay_log_is_in_causal_order():
    r = prune(
        document={},
        replica_a=[op("alice", 1, {"alice": 1}, "/a", "set", 1)],
        replica_b=[
            op("bob", 1, {"alice": 1, "bob": 1}, "/b", "set", 1),
            op("carol", 1, {"carol": 1}, "/c", "set", 1),
        ],
        exclude=[("bob", 1)],
    )
    body = r.json()
    # bob explicitly dropped; alice stays; carol is concurrent and kept;
    # causal/arbitration order is ascending (actor, seq)
    assert [(o["actor"], o["seq"]) for o in body["replay_log"]] == [
        ("alice", 1),
        ("carol", 1),
    ]


def test_replay_log_validates_against_apply_rules():
    r = prune(
        document={},
        version={"zoe": 1},
        replica_a=[
            op("alice", 1, {"alice": 1, "zoe": 1}, "/a", "set", 1),
            op("alice", 2, {"alice": 2, "zoe": 1}, "/a2", "set", 1),
        ],
        replica_b=[op("bob", 1, {"alice": 2, "bob": 1}, "/b", "set", 1)],
        exclude=[("alice", 2)],
    )
    body = r.json()
    again = replay({}, {"zoe": 1}, body["replay_log"])
    assert again.status_code == 200
    assert again.json()["document"] == body["document"]
    assert again.json()["version"] == body["version"]


# ---------------------------------------------------------------------------
# /apply supports exclusion too
# ---------------------------------------------------------------------------

def test_apply_supports_exclusion():
    r = prune_apply(
        document={"a": 0},
        operations=[
            op("alice", 1, {"alice": 1}, "/a", "set", 1),
            op("alice", 2, {"alice": 2}, "/a", "set", 2),
        ],
        exclude=[("alice", 1)],
    )
    body = r.json()
    assert body["document"] == {"a": 0}
    assert body["version"] == {}
    assert body["replay_log"] == []
    assert [(e["seq"], e["mode"]) for e in body["excluded"]] == [
        (1, "explicit"),
        (2, "cascade"),
    ]


def test_exclude_operations_alias_accepted():
    for endpoint, logfield in (("/merge", "replica_a"), ("/apply", "operations")):
        r = client.post(endpoint, json={
            "document": {},
            logfield: [op("alice", 1, {"alice": 1}, "/x", "set", 1)],
            "exclude_operations": [{"actor": "alice", "seq": 1}],
        })
        assert r.status_code == 200
        assert r.json()["document"] == {}
        assert r.json()["excluded"][0]["mode"] == "explicit"


def test_duplicate_ids_in_exclude_list_deduped():
    r = prune(
        document={},
        replica_a=[op("alice", 1, {"alice": 1}, "/x", "set", 1)],
        exclude=[("alice", 1), ("alice", 1)],
    )
    assert r.status_code == 200
    assert len(r.json()["excluded"]) == 1


# ---------------------------------------------------------------------------
# validation rules run on the FULL log first
# ---------------------------------------------------------------------------

def test_missing_excluded_id_is_rejected():
    r = prune(
        document={},
        replica_a=[op("alice", 1, {"alice": 1}, "/x", "set", 1)],
        exclude=[("bob", 1)],
    )
    assert r.status_code == 422
    assert "excluded operation bob:1 is not present" in r.json()["detail"]


def test_invalid_excluded_operation_still_rejected_by_full_validation():
    # own position missing from the version vector -> 422 even though excluded
    r = prune(
        document={},
        replica_a=[op("alice", 1, {"bob": 9}, "/x", "set", 1)],
        exclude=[("alice", 1)],
    )
    assert r.status_code == 422
    assert "own position" in r.json()["detail"]


def test_sequence_gap_not_healed_by_exclusion():
    r = prune(
        document={},
        replica_a=[op("alice", 2, {"alice": 2}, "/x", "set", 1)],
        exclude=[("alice", 2)],
    )
    assert r.status_code == 422
    assert "not contiguous" in r.json()["detail"]


def test_conflicting_duplicate_not_healed_by_exclusion():
    r = prune(
        document={},
        replica_a=[
            op("alice", 1, {"alice": 1}, "/x", "set", 1),
            op("alice", 1, {"alice": 1}, "/x", "set", 2),
        ],
        exclude=[("alice", 1)],
    )
    assert r.status_code == 422
    assert "different payloads" in r.json()["detail"]


def test_missing_causal_dependency_not_healed_by_exclusion():
    r = prune(
        document={},
        replica_b=[op("bob", 1, {"alice": 5, "bob": 1}, "/x", "set", 1)],
        exclude=[("bob", 1)],
    )
    assert r.status_code == 422
    assert "depends on alice:5" in r.json()["detail"]


def test_duplicates_still_counted_from_full_submission():
    shared = op("alice", 1, {"alice": 1}, "/x", "set", 1)
    r = prune(
        document={},
        replica_a=[shared],
        replica_b=[shared, op("bob", 1, {"bob": 1}, "/y", "set", 1)],
        exclude=[("alice", 1)],
    )
    body = r.json()
    assert body["duplicates"] == 1
    assert body["applied"] == 1
    assert body["document"] == {"y": 1}


def test_excluded_id_must_be_an_actual_log_operation_not_initial_version():
    r = prune(
        document={},
        version={"alice": 1},
        replica_a=[op("bob", 1, {"alice": 1, "bob": 1}, "/x", "set", 1)],
        exclude=[("alice", 1)],
    )
    assert r.status_code == 422
    assert "not present" in r.json()["detail"]


def test_malformed_exclude_entry_is_422():
    r = client.post("/merge", json={
        "document": {},
        "replica_a": [op("alice", 1, {"alice": 1}, "/x", "set", 1)],
        "exclude": [{"actor": "alice"}],  # seq missing
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# backward compatibility: no exclude -> response shape unchanged
# ---------------------------------------------------------------------------

def test_merge_without_exclude_keeps_original_response_shape():
    r = client.post("/merge", json={
        "document": {},
        "replica_a": [op("alice", 1, {"alice": 1}, "/x", "set", 1)],
    })
    assert set(r.json().keys()) == BASE_KEYS


def test_apply_without_exclude_keeps_original_response_shape():
    r = client.post("/apply", json={
        "document": {},
        "operations": [op("alice", 1, {"alice": 1}, "/x", "set", 1)],
    })
    assert set(r.json().keys()) == BASE_KEYS


def test_empty_exclude_list_is_equivalent_to_omission():
    r = client.post("/merge", json={
        "document": {},
        "replica_a": [op("alice", 1, {"alice": 1}, "/x", "set", 1)],
        "exclude": [],
    })
    assert set(r.json().keys()) == BASE_KEYS
    assert r.json()["applied"] == 1
