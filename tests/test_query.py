"""End-to-end tests for the restricted JSONPath query API (POST /query)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

STORE = {
    "store": {
        "book": [
            {
                "category": "reference",
                "author": "Nigel Rees",
                "title": "Sayings of the Century",
                "price": 8.95,
            },
            {
                "category": "fiction",
                "author": "Evelyn Waugh",
                "title": "Sword of Honour",
                "price": 12.99,
            },
            {
                "category": "fiction",
                "author": "Herman Melville",
                "title": "Moby Dick",
                "isbn": "0-553-21311-3",
                "price": 8.99,
            },
            {
                "category": "fiction",
                "author": "J. R. R. Tolkien",
                "title": "The Lord of the Rings",
                "isbn": "0-395-19395-8",
                "price": 22.99,
            },
        ],
        "bicycle": {"color": "red", "price": 19.95},
    },
    "expensive": 10,
}


def query(document, *queries):
    """POST /query with (query_id, path) pairs."""
    return client.post(
        "/query",
        json={
            "document": document,
            "queries": [
                {"query_id": qid, "path": path} for qid, path in queries
            ],
        },
    )


def matches(resp, index=0):
    assert resp.status_code == 200, resp.json()
    return resp.json()["results"][index]["matches"]


# ---------------------------------------------------------------------------
# basic selectors
# ---------------------------------------------------------------------------

def test_root_only():
    r = query({"a": 1}, ("q1", "$"))
    assert matches(r) == [{"path": "", "value": {"a": 1}}]


def test_root_scalar_document():
    r = query(42, ("q1", "$"))
    assert matches(r) == [{"path": "", "value": 42}]


def test_dot_child_and_nested_path():
    r = query(
        STORE,
        ("q1", "$.expensive"),
        ("q2", "$.store.bicycle.color"),
    )
    assert matches(r, 0) == [{"path": "/expensive", "value": 10}]
    assert matches(r, 1) == [{"path": "/store/bicycle/color", "value": "red"}]


def test_missing_path_yields_empty_matches():
    r = query(STORE, ("q1", "$.store.missing.deeper"))
    assert matches(r) == []


def test_child_on_array_or_scalar_selects_nothing():
    r = query(
        STORE,
        ("q1", "$.store.book.author"),  # book is an array
        ("q2", "$.expensive.foo"),  # expensive is a scalar
    )
    assert matches(r, 0) == []
    assert matches(r, 1) == []


def test_bracket_keys_both_quotes():
    r = query(
        STORE,
        ("q1", "$['store']['bicycle']['price']"),
        ("q2", '$["store"]["bicycle"]["color"]'),
    )
    assert matches(r, 0) == [{"path": "/store/bicycle/price", "value": 19.95}]
    assert matches(r, 1) == [{"path": "/store/bicycle/color", "value": "red"}]


def test_bracket_key_escapes():
    doc = {"it's": 1, 'quote"key': 2, "back\\slash": 3, "tab\tk": 4, "é": 5}
    r = query(
        doc,
        ("q1", "$['it\\'s']"),
        ("q2", '$["quote\\"key"]'),
        ("q3", "$['back\\\\slash']"),
        ("q4", "$['tab\\tk']"),
        ("q5", "$['\\u00e9']"),
    )
    assert matches(r, 0) == [{"path": "/it's", "value": 1}]
    assert matches(r, 1) == [{"path": "/quote\"key", "value": 2}]
    assert matches(r, 2) == [{"path": "/back\\slash", "value": 3}]
    assert matches(r, 3) == [{"path": "/tab\tk", "value": 4}]
    assert matches(r, 4) == [{"path": "/é", "value": 5}]


def test_empty_key():
    r = query({"": "x"}, ("q1", "$['']"))
    assert matches(r) == [{"path": "/", "value": "x"}]


def test_output_paths_use_json_pointer_escaping():
    doc = {"a/b": {"~tilde": 1}}
    r = query(doc, ("q1", "$['a/b']['~tilde']"))
    assert matches(r) == [{"path": "/a~1b/~0tilde", "value": 1}]


def test_array_index_and_negative_index():
    r = query(
        STORE,
        ("q1", "$.store.book[0].author"),
        ("q2", "$.store.book[-1].title"),
        ("q3", "$.store.book[10]"),  # out of range
    )
    assert matches(r, 0) == [
        {"path": "/store/book/0/author", "value": "Nigel Rees"}
    ]
    assert matches(r, 1) == [
        {"path": "/store/book/3/title", "value": "The Lord of the Rings"}
    ]
    assert matches(r, 2) == []


def test_index_on_object_selects_nothing():
    r = query(STORE, ("q1", "$.store.bicycle[0]"))
    assert matches(r) == []


def test_wildcard_over_array():
    r = query(STORE, ("q1", "$.store.book[*].author"))
    assert matches(r) == [
        {"path": "/store/book/0/author", "value": "Nigel Rees"},
        {"path": "/store/book/1/author", "value": "Evelyn Waugh"},
        {"path": "/store/book/2/author", "value": "Herman Melville"},
        {"path": "/store/book/3/author", "value": "J. R. R. Tolkien"},
    ]


def test_wildcard_over_object_preserves_member_order():
    r = query(STORE, ("q1", "$.store.bicycle[*]"))
    assert matches(r) == [
        {"path": "/store/bicycle/color", "value": "red"},
        {"path": "/store/bicycle/price", "value": 19.95},
    ]


# ---------------------------------------------------------------------------
# recursive descent
# ---------------------------------------------------------------------------

def test_recursive_name():
    r = query(STORE, ("q1", "$..author"))
    assert [m["path"] for m in matches(r)] == [
        "/store/book/0/author",
        "/store/book/1/author",
        "/store/book/2/author",
        "/store/book/3/author",
    ]


def test_recursive_name_includes_root_object():
    r = query({"name": "root", "child": {"name": "kid"}}, ("q1", "$..name"))
    assert matches(r) == [
        {"path": "/name", "value": "root"},
        {"path": "/child/name", "value": "kid"},
    ]


def test_recursive_wildcard_returns_all_descendants_in_preorder():
    r = query({"a": {"b": 1}, "c": [2]}, ("q1", "$..*"))
    assert matches(r) == [
        {"path": "/a", "value": {"b": 1}},
        {"path": "/a/b", "value": 1},
        {"path": "/c", "value": [2]},
        {"path": "/c/0", "value": 2},
    ]


def test_results_are_sorted_in_document_preorder():
    # DFS evaluation would emit "shallow" before "deep"; pre-order sorting
    # must place the deeper node first because "z" precedes "name" in "a".
    doc = {"a": {"z": {"name": "deep"}, "name": "shallow"}}
    r = query(doc, ("q1", "$..name"))
    assert matches(r) == [
        {"path": "/a/z/name", "value": "deep"},
        {"path": "/a/name", "value": "shallow"},
    ]


def test_duplicate_paths_are_reported_once():
    # $..* yields both /x and /x/y; ..name from each reaches /x/y/name twice.
    doc = {"x": {"name": "n1", "y": {"name": "n2"}}}
    r = query(doc, ("q1", "$..*..name"))
    assert matches(r) == [
        {"path": "/x/name", "value": "n1"},
        {"path": "/x/y/name", "value": "n2"},
    ]


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------

def test_filter_string_equality():
    r = query(STORE, ("q1", '$.store.book[?(@.category == "fiction")].title'))
    assert matches(r) == [
        {"path": "/store/book/1/title", "value": "Sword of Honour"},
        {"path": "/store/book/2/title", "value": "Moby Dick"},
        {"path": "/store/book/3/title", "value": "The Lord of the Rings"},
    ]


def test_filter_string_inequality():
    r = query(STORE, ("q1", '$.store.book[?(@.category != "fiction")].title'))
    assert matches(r) == [
        {"path": "/store/book/0/title", "value": "Sayings of the Century"}
    ]


def test_filter_numeric_ordering():
    r = query(
        STORE,
        ("lt", "$.store.book[?(@.price < 9)].title"),
        ("le", "$.store.book[?(@.price <= 8.95)].title"),
        ("gt", "$.store.book[?(@.price > 20)].title"),
        ("ge", "$.store.book[?(@.price >= 12.99)].title"),
    )
    assert [m["value"] for m in matches(r, 0)] == [
        "Sayings of the Century",
        "Moby Dick",
    ]
    assert [m["value"] for m in matches(r, 1)] == ["Sayings of the Century"]
    assert [m["value"] for m in matches(r, 2)] == ["The Lord of the Rings"]
    assert [m["value"] for m in matches(r, 3)] == [
        "Sword of Honour",
        "The Lord of the Rings",
    ]


def test_filter_whitespace_and_no_whitespace():
    r = query(
        STORE,
        ("q1", "$.store.book[?( @.price>=12.99 )].title"),
        ("q2", "$.store.book[?(@.price>=12.99)].title"),
    )
    assert matches(r, 0) == matches(r, 1)


def test_filter_equality_is_json_type_strict():
    doc = {
        "items": [
            {"v": 1},
            {"v": True},
            {"v": "1"},
            {"v": None},
            {"v": 1.0},
            {"v": [1]},
            {},
        ]
    }
    r = query(
        doc,
        ("num", "$.items[?(@.v == 1)]"),
        ("bool", "$.items[?(@.v == true)]"),
        ("str", '$.items[?(@.v == "1")]'),
        ("null", "$.items[?(@.v == null)]"),
        ("ne", "$.items[?(@.v != 1)]"),
    )
    # 1 and 1.0 are both JSON numbers; true and "1" are different types
    assert [m["path"] for m in matches(r, 0)] == ["/items/0", "/items/4"]
    assert [m["path"] for m in matches(r, 1)] == ["/items/1"]
    assert [m["path"] for m in matches(r, 2)] == ["/items/2"]
    assert [m["path"] for m in matches(r, 3)] == ["/items/3"]
    # != still requires a present scalar field: [1] and the missing field miss
    assert [m["path"] for m in matches(r, 4)] == [
        "/items/1",
        "/items/2",
        "/items/3",
    ]


def test_filter_ordering_only_compares_numbers():
    doc = {"items": [{"v": 1}, {"v": "x"}, {"v": True}, {"v": 2.5}]}
    r = query(
        doc,
        ("lt", "$.items[?(@.v < 2)]"),
        ("str-literal", '$.items[?(@.v > "a")]'),
        ("bool-literal", "$.items[?(@.v < true)]"),
    )
    assert [m["path"] for m in matches(r, 0)] == ["/items/0"]
    assert matches(r, 1) == []
    assert matches(r, 2) == []


def test_filter_missing_or_nonscalar_field_never_matches():
    doc = {
        "items": [
            {"x": 1},
            {"y": 1},
            {"x": [1]},
            {"x": {"a": 1}},
            {"x": 2},
        ]
    }
    r = query(
        doc,
        ("ne", "$.items[?(@.x != 5)]"),
        ("eq", "$.items[?(@.x == 1)]"),
    )
    assert [m["path"] for m in matches(r, 0)] == ["/items/0", "/items/4"]
    assert [m["path"] for m in matches(r, 1)] == ["/items/0"]


def test_filter_only_applies_to_arrays():
    r = query(
        STORE,
        ("obj", '$.store.bicycle[?(@.color == "red")]'),
        ("scalar", "$.expensive[?(@.x == 1)]"),
    )
    assert matches(r, 0) == []
    assert matches(r, 1) == []


def test_filter_on_root_array():
    r = query([{"a": 1}, {"a": 2}, {"b": 3}], ("q1", "$[?(@.a >= 2)]"))
    assert matches(r) == [{"path": "/1", "value": {"a": 2}}]


def test_filter_string_literal_may_contain_brackets():
    doc = {"items": [{"name": "a)b]c"}, {"name": "other"}]}
    r = query(doc, ("q1", '$.items[?(@.name == "a)b]c")]'))
    assert matches(r) == [{"path": "/items/0", "value": {"name": "a)b]c"}}]


def test_filter_selects_elements_not_field_values():
    r = query(STORE, ("q1", "$.store.book[?(@.isbn)]"))  # existence not supported
    assert r.status_code == 422
    r = query(STORE, ("q1", "$.store.book[?(@.price < 9)]"))
    assert matches(r) == [
        {
            "path": "/store/book/0",
            "value": {
                "category": "reference",
                "author": "Nigel Rees",
                "title": "Sayings of the Century",
                "price": 8.95,
            },
        },
        {
            "path": "/store/book/2",
            "value": {
                "category": "fiction",
                "author": "Herman Melville",
                "title": "Moby Dick",
                "isbn": "0-553-21311-3",
                "price": 8.99,
            },
        },
    ]


# ---------------------------------------------------------------------------
# request shape
# ---------------------------------------------------------------------------

def test_results_follow_request_order_not_query_id_order():
    r = query({"a": 1, "b": 2}, ("zeta", "$.a"), ("alpha", "$.b"))
    body = r.json()
    assert [res["query_id"] for res in body["results"]] == ["zeta", "alpha"]
    assert body["results"][0]["matches"] == [{"path": "/a", "value": 1}]
    assert body["results"][1]["matches"] == [{"path": "/b", "value": 2}]


def test_query_field_alias_is_accepted():
    resp = client.post(
        "/query",
        json={
            "document": {"a": 1},
            "queries": [{"query_id": "q1", "query": "$.a"}],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["matches"] == [
        {"path": "/a", "value": 1}
    ]


def test_document_is_not_modified_by_queries():
    doc = {"a": {"b": [1, 2, {"c": 3}]}}
    r = query(doc, ("all", "$..*"), ("root", "$"))
    assert matches(r, 1) == [{"path": "", "value": doc}]


def test_queries_do_not_execute_scripts():
    # anything outside the restricted grammar is rejected, never evaluated
    r = query({"a": 1}, ("q1", "$[?(1)]"))
    assert r.status_code == 422
    r = query({"a": [{"b": 1}]}, ("q1", "$.a[?(@.b == 1)].constructor"))
    assert r.status_code == 200
    assert matches(r) == []  # plain (missing) member lookup, no code runs


# ---------------------------------------------------------------------------
# validation failures -> 422
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_path",
    [
        "",  # empty
        "store.book",  # missing $
        "$.",
        "$..",
        "$...",
        "$.*",  # dot wildcard is not part of the subset
        "$[",
        "$[]",
        "$['a'",
        "$['a'x]",
        "$['a\"]",  # escape eats the closing quote -> unterminated
        "$['\\q']",  # unknown escape
        "$[abc]",
        "$[1.5]",
        "$[01]",
        "$[0:2]",  # slices not supported
        "$[0,1]",  # unions not supported
        "$.a b",  # stray whitespace
        "$x",
        "$[?(@.a)]",  # existence check not supported
        "$[?(@.a=1)]",  # single =
        "$[?(@.a ==)]",  # missing literal
        "$[?(@.a == 1]",  # missing )
        "$[?(@.a == 1)",  # missing ]
        "$[?(@.a == 'x')]",  # single-quoted literal is not JSON
        "$[?(@.a == {})]",  # object literal not a scalar
        "$[?(@.a == [1])]",  # array literal not a scalar
        "$[?(@.a == 01)]",  # leading zero is not valid JSON
        "$[?(@.a == truex)]",
        "$[?(@ == 1)]",  # @ must be followed by .field
        "$[?(a.b == 1)]",  # missing @
        "$[?(@.a ~ 1)]",  # unknown operator
        "$..['a']",  # recursive descent takes a name or * only
        "$.a[0]x",
    ],
)
def test_syntax_errors_are_rejected(bad_path):
    r = query({"a": [1, 2]}, ("q1", bad_path))
    assert r.status_code == 422
    if bad_path:  # empty paths are rejected by schema validation instead
        assert "q1" in r.json()["detail"]


def test_duplicate_query_id_rejected():
    r = query({"a": 1}, ("q1", "$"), ("q1", "$.a"))
    assert r.status_code == 422
    assert "duplicate query_id" in r.json()["detail"]


def test_query_count_bounds():
    r = client.post("/query", json={"document": {}, "queries": []})
    assert r.status_code == 422
    r = client.post(
        "/query",
        json={
            "document": {},
            "queries": [
                {"query_id": f"q{i}", "path": "$"} for i in range(21)
            ],
        },
    )
    assert r.status_code == 422


def test_node_budget_exceeded():
    doc = {"arr": list(range(20000))}
    r = query(doc, ("big", "$..*"))
    assert r.status_code == 422
    assert "10000" in r.json()["detail"]


def test_node_budget_is_per_query():
    doc = {"arr": list(range(100))}
    r = query(doc, ("ok", "$..*"))
    assert r.status_code == 200
    assert len(matches(r)) == 101  # the array itself plus 100 elements


def test_missing_query_id_rejected():
    resp = client.post(
        "/query",
        json={"document": {}, "queries": [{"path": "$"}]},
    )
    assert resp.status_code == 422


def test_healthz_still_ok():
    assert client.get("/healthz").json() == {"status": "ok"}
