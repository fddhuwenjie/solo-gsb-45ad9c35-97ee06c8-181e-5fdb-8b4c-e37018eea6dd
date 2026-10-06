"""End-to-end tests for the cross-document reference API (POST /resolve)."""
from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient

from app.main import app
from app.models import ResolveDocument
from app.resolve import resolve_documents

client = TestClient(app)


def doc(document_id, document):
    return {"document_id": document_id, "document": document}


def resolve(documents, root):
    return client.post("/resolve", json={"documents": documents, "root": root})


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------

def test_plain_document_without_references():
    r = resolve([doc("a", {"x": 1, "y": [True, None]})], "a")
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] == {"x": 1, "y": [True, None]}
    assert body["dependencies"] == ["a"]
    assert body["sources"] == {
        "/x": "a#/x",
        "/y/0": "a#/y/0",
        "/y/1": "a#/y/1",
    }


def test_root_pointer_forms():
    docs = [doc("a", {"sub": {"k": "v"}})]
    assert resolve(docs, "a").json()["resolved"] == {"sub": {"k": "v"}}
    assert resolve(docs, "a#").json()["resolved"] == {"sub": {"k": "v"}}
    r = resolve(docs, "a#/sub")
    assert r.status_code == 200
    assert r.json()["resolved"] == {"k": "v"}
    assert r.json()["sources"] == {"/k": "a#/sub/k"}


def test_scalar_root_document():
    r = resolve([doc("a", 5)], "a")
    body = r.json()
    assert body["resolved"] == 5
    assert body["dependencies"] == ["a"]
    assert body["sources"] == {"": "a"}


def test_nested_ref_same_document():
    r = resolve([doc("a", {"y": 5, "x": {"$ref": "#/y"}})], "a")
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] == {"y": 5, "x": 5}
    assert body["dependencies"] == ["a"]
    assert body["sources"] == {"/x": "a#/y", "/y": "a#/y"}


def test_ref_to_current_document_root_is_cycle():
    r = resolve([doc("a", {"wrap": {"$ref": "#"}, "in": 1})], "a")
    assert r.status_code == 422
    assert "circular reference" in r.json()["detail"]


def test_cross_document_ref():
    r = resolve(
        [doc("a", {"x": {"$ref": "b#/y"}}), doc("b", {"y": {"k": 1}})], "a"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] == {"x": {"k": 1}}
    assert body["dependencies"] == ["a", "b"]
    assert body["sources"] == {"/x/k": "b#/y/k"}


def test_nested_ref_to_whole_document():
    r = resolve([doc("a", {"x": {"$ref": "b#"}}), doc("b", {"k": 1})], "a")
    body = r.json()
    assert body["resolved"] == {"x": {"k": 1}}
    assert body["sources"] == {"/x/k": "b#/k"}


def test_ref_to_array_without_overlay():
    r = resolve([doc("a", {"x": {"$ref": "b#/y"}}), doc("b", {"y": [1, 2]})], "a")
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] == {"x": [1, 2]}
    assert body["sources"] == {"/x/0": "b#/y/0", "/x/1": "b#/y/1"}


def test_cross_document_overlay_merge():
    r = resolve(
        [
            doc(
                "base",
                {
                    "service": {
                        "image": "app:1.0",
                        "ports": [80],
                        "limits": {"cpu": "500m", "mem": "256Mi"},
                    }
                },
            ),
            doc(
                "prod",
                {
                    "service": {
                        "$ref": "base#/service",
                        "image": "app:2.0",
                        "limits": {"mem": "1Gi"},
                    }
                },
            ),
        ],
        "prod",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] == {
        "service": {
            "image": "app:2.0",
            "ports": [80],
            "limits": {"cpu": "500m", "mem": "1Gi"},
        }
    }
    assert body["dependencies"] == ["prod", "base"]
    assert body["sources"] == {
        "/service/image": "prod#/service/image",
        "/service/limits/cpu": "base#/service/limits/cpu",
        "/service/limits/mem": "prod#/service/limits/mem",
        "/service/ports/0": "base#/service/ports/0",
    }


def test_overlay_merges_objects_recursively():
    r = resolve(
        [
            doc("base", {"o": {"a": {"x": 1, "y": 2}, "b": 1}}),
            doc("app", {"o": {"$ref": "base#/o", "a": {"y": 20, "z": 3}}}),
        ],
        "app",
    )
    assert r.status_code == 200
    assert r.json()["resolved"]["o"] == {"a": {"x": 1, "y": 20, "z": 3}, "b": 1}


def test_overlay_replaces_arrays_and_scalars_wholesale():
    r = resolve(
        [
            doc("base", {"v": {"arr": [1, 2], "n": 1, "s": "x"}}),
            doc("app", {"v": {"$ref": "base#/v", "arr": [3], "n": 2}}),
        ],
        "app",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"]["v"] == {"arr": [3], "n": 2, "s": "x"}
    assert body["sources"] == {
        "/v/arr/0": "app#/v/arr/0",
        "/v/n": "app#/v/n",
        "/v/s": "base#/v/s",
    }


def test_overlay_values_are_resolved():
    r = resolve(
        [
            doc("a", {"x": {"$ref": "b#/y", "m": {"$ref": "c#/z"}}}),
            doc("b", {"y": {"k": 1}}),
            doc("c", {"z": 2}),
        ],
        "a",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] == {"x": {"k": 1, "m": 2}}
    assert body["dependencies"] == ["a", "b", "c"]
    assert body["sources"] == {"/x/k": "b#/y/k", "/x/m": "c#/z"}


def test_diamond_reuse_is_not_a_cycle():
    r = resolve(
        [
            doc("a", {"p": {"$ref": "b#/z"}, "q": {"$ref": "b#/z"}}),
            doc("b", {"z": {"k": 1}}),
        ],
        "a",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] == {"p": {"k": 1}, "q": {"k": 1}}
    assert body["dependencies"] == ["a", "b"]
    assert body["sources"] == {"/p/k": "b#/z/k", "/q/k": "b#/z/k"}


def test_dependencies_first_visit_order():
    r = resolve(
        [
            doc(
                "a",
                {
                    "one": {"$ref": "c#/v"},
                    "two": {"$ref": "b#/v"},
                    "three": {"$ref": "c#/v"},
                },
            ),
            doc("b", {"v": 1}),
            doc("c", {"v": 2}),
        ],
        "a",
    )
    assert r.json()["dependencies"] == ["a", "c", "b"]


def test_sources_sorted_by_output_pointer():
    r = resolve([doc("a", {"b": 1, "a": 2})], "a")
    assert list(r.json()["sources"].keys()) == ["/a", "/b"]


def test_empty_containers_are_leaves():
    r = resolve([doc("a", {"e": {}, "l": []})], "a")
    body = r.json()
    assert body["resolved"] == {"e": {}, "l": []}
    assert body["sources"] == {"/e": "a#/e", "/l": "a#/l"}


def test_rfc6901_escaping():
    r = resolve(
        [doc("a", {"a/b": {"t~k": 1}, "x": {"$ref": "#/a~1b/t~0k"}})], "a"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"]["x"] == 1
    assert body["sources"] == {
        "/a~1b/t~0k": "a#/a~1b/t~0k",
        "/x": "a#/a~1b/t~0k",
    }


def test_resolution_does_not_mutate_input_documents():
    docs = [
        ResolveDocument(
            document_id="base", document={"v": {"arr": [1, 2], "n": 1}}
        ),
        ResolveDocument(
            document_id="app", document={"v": {"$ref": "base#/v", "n": 9}}
        ),
    ]
    snapshot = deepcopy([d.document for d in docs])
    result = resolve_documents(docs, "app")
    assert result["resolved"]["v"] == {"arr": [1, 2], "n": 9}
    assert [d.document for d in docs] == snapshot
    # mutating the result must not bleed back into the inputs either
    result["resolved"]["v"]["arr"].append(99)
    assert docs[0].document["v"]["arr"] == [1, 2]


# ---------------------------------------------------------------------------
# validation errors (422)
# ---------------------------------------------------------------------------

def test_documents_count_limits():
    assert resolve([], "a").status_code == 422
    too_many = [doc(f"d{i}", i) for i in range(51)]
    assert resolve(too_many, "d0").status_code == 422
    fifty = [doc(f"d{i}", i) for i in range(50)]
    assert resolve(fifty, "d0").status_code == 200


def test_duplicate_document_id_422():
    r = resolve([doc("a", 1), doc("a", 2)], "a")
    assert r.status_code == 422
    assert "duplicate document_id" in r.json()["detail"]


def test_root_must_include_document_id():
    assert resolve([doc("a", {"x": 1})], "#/x").status_code == 422
    assert resolve([doc("a", {"x": 1})], "#").status_code == 422


def test_missing_root_document_422():
    r = resolve([doc("a", {})], "nope")
    assert r.status_code == 422
    assert "'nope'" in r.json()["detail"]


def test_missing_document_422_with_chain():
    r = resolve([doc("a", {"x": {"$ref": "b#/y"}})], "a")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "'b'" in detail
    assert "a -> b#/y" in detail


def test_nested_ref_without_hash_422():
    r = resolve([doc("a", {"x": {"$ref": "b"}}), doc("b", {"y": 1})], "a")
    assert r.status_code == 422
    assert "#" in r.json()["detail"]


def test_ref_value_must_be_string():
    r = resolve([doc("a", {"x": {"$ref": 5}})], "a")
    assert r.status_code == 422


def test_invalid_pointer_escape_422():
    r = resolve([doc("a", {"x": {"$ref": "#/a~2b"}})], "a")
    assert r.status_code == 422
    assert "invalid JSON Pointer" in r.json()["detail"]


def test_pointer_must_start_with_slash_422():
    r = resolve([doc("a", {"x": {"$ref": "#abc"}})], "a")
    assert r.status_code == 422
    assert "invalid JSON Pointer" in r.json()["detail"]


def test_invalid_pointer_in_root_422():
    r = resolve([doc("a", {"x": 1})], "a#x")
    assert r.status_code == 422
    assert "invalid JSON Pointer" in r.json()["detail"]


def test_missing_object_key_422():
    r = resolve([doc("a", {"x": {"$ref": "#/nope"}})], "a")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "not found" in detail
    assert "a#/nope" in detail


def test_missing_array_index_422():
    r = resolve([doc("a", {"arr": [1], "x": {"$ref": "#/arr/5"}})], "a")
    assert r.status_code == 422
    assert "not found" in r.json()["detail"]


def test_traverse_through_scalar_422():
    r = resolve([doc("a", {"x": 1, "y": {"$ref": "#/x/y"}})], "a")
    assert r.status_code == 422
    assert "not found" in r.json()["detail"]


def test_overlay_on_array_target_422():
    r = resolve(
        [doc("a", {"x": {"$ref": "b#/y", "k": 1}}), doc("b", {"y": [1, 2]})],
        "a",
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "non-object" in detail
    assert "a -> b#/y" in detail


def test_overlay_on_scalar_target_422():
    r = resolve(
        [doc("a", {"x": {"$ref": "b#/y", "k": 1}}), doc("b", {"y": 7})], "a"
    )
    assert r.status_code == 422
    assert "non-object" in r.json()["detail"]


# ---------------------------------------------------------------------------
# cycle detection
# ---------------------------------------------------------------------------

def test_self_reference_cycle_422():
    r = resolve([doc("a", {"x": {"$ref": "#/x"}})], "a")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "circular reference" in detail
    assert "a -> a#/x -> a#/x" in detail


def test_root_self_cycle_422():
    r = resolve([doc("a", {"$ref": "a#"})], "a")
    assert r.status_code == 422
    assert "a -> a" in r.json()["detail"]


def test_cross_document_cycle_422():
    r = resolve(
        [
            doc("a", {"x": {"$ref": "b#/y"}}),
            doc("b", {"y": {"$ref": "a#/x"}}),
        ],
        "a",
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "circular reference" in detail
    assert "a#/x" in detail
    assert "b#/y" in detail


def test_cycle_through_overlay_422():
    r = resolve(
        [doc("a", {"x": {"$ref": "b#/y", "k": {"$ref": "#/x"}}}), doc("b", {"y": {}})],
        "a",
    )
    assert r.status_code == 422
    assert "circular reference" in r.json()["detail"]
