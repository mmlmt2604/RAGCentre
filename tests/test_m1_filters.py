from __future__ import annotations

import json
import random
import sqlite3
from typing import Any

import pytest

from ragcentre.errors import InvalidFilter
from ragcentre.models.filters import And, Cond, Or, matches, parse_filter, schema_types, to_json
from ragcentre.stores.filter_sql import compile_postgres, compile_sqlite
from ragcentre.testing.random_data import full_schema, random_filter, random_metadata

SCHEMA = full_schema()


def test_shorthand_and_implicit_and() -> None:
    f = parse_filter({"lang": "en", "year": {"$gte": 2020, "$lt": 2024}}, SCHEMA)
    assert f == And((Cond("lang", "$eq", "en"), Cond("year", "$gte", 2020), Cond("year", "$lt", 2024)))


def test_single_operand_combinators_collapse() -> None:
    assert parse_filter({"$and": [{"lang": "en"}]}, SCHEMA) == Cond("lang", "$eq", "en")
    assert parse_filter({"$or": [{"$and": [{"lang": "en"}]}]}, SCHEMA) == Cond("lang", "$eq", "en")
    f = parse_filter({"$or": [{"lang": "en"}, {"lang": "de"}]}, SCHEMA)
    assert isinstance(f, Or) and len(f.items) == 2


def test_empty_is_no_filter() -> None:
    assert parse_filter(None, SCHEMA) is None
    assert parse_filter({}, SCHEMA) is None
    assert matches(None, {}) is True


@pytest.mark.parametrize(
    "raw",
    [
        {"unknown": 1},
        {"lang": 1},
        {"year": "2020"},
        {"year": 2020.5},
        {"year": True},
        {"score": True},
        {"public": 1},
        {"lang": {"$gt": "a"}},
        {"public": {"$lt": True}},
        {"lang": {"$in": []}},
        {"lang": {"$in": "en"}},
        {"lang": {"$in": ["en", 1]}},
        {"lang": {"$in": ["x"] * 101}},
        {"lang": {"$regex": "x"}},
        {"lang": {}},
        {"$and": []},
        {"$or": {}},
        {"$not": [{"lang": "en"}]},
        {"$and": [{"$and": [{"$and": [{"$and": [{"$and": [{"lang": "en"}]}]}]}]}]},
        [1],
        {"score": float("nan")},
    ],
)
def test_invalid_filters(raw: Any) -> None:
    with pytest.raises(InvalidFilter) as exc:
        parse_filter(raw, SCHEMA)
    assert exc.value.error_code.value == "invalid_filter"


def test_depth_five_is_allowed() -> None:
    raw: dict[str, Any] = {"lang": "en"}
    for _ in range(4):
        raw = {"$and": [raw, {"year": 2020}]}
    assert parse_filter(raw, SCHEMA) is not None


def test_int_accepted_for_float_field() -> None:
    assert parse_filter({"score": 1}, SCHEMA) == Cond("score", "$eq", 1)


def test_schema_types_adds_document_id() -> None:
    assert schema_types({"fields": {"lang": {"type": "str", "default": "en"}}}) == {"lang": "str", "document_id": "str"}


def test_matches_semantics() -> None:
    row = {"lang": "en", "year": 2020, "score": 0.5, "public": True, "document_id": "d"}
    assert matches(parse_filter({"public": True}, SCHEMA), row)
    assert not matches(parse_filter({"public": False}, SCHEMA), row)
    assert matches(parse_filter({"year": {"$nin": [2019, 2021]}}, SCHEMA), row)
    assert matches(parse_filter({"$or": [{"lang": "de"}, {"score": {"$lte": 0.5}}]}, SCHEMA), row)
    assert not matches(parse_filter({"lang": {"$ne": "en"}}, SCHEMA), row)
    # A missing field: only $ne and $nin hold.
    assert matches(parse_filter({"lang": {"$ne": "en"}}, SCHEMA), {})
    assert not matches(parse_filter({"year": {"$gt": 0}}, SCHEMA), {})


def test_to_json_roundtrip() -> None:
    raw = {"$or": [{"lang": "en"}, {"year": {"$in": [1, 2]}, "public": False}]}
    f = parse_filter(raw, SCHEMA)
    assert f is not None
    assert parse_filter(to_json(f), SCHEMA) == f


def test_sqlite_compiler_agrees_with_matches_on_random_filters() -> None:
    rng = random.Random(1234)
    rows = [random_metadata(rng) for _ in range(200)]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, metadata TEXT NOT NULL)")
    conn.executemany("INSERT INTO t (id, metadata) VALUES (?, ?)", [(i, json.dumps(r)) for i, r in enumerate(rows)])
    nonempty = 0
    for _ in range(500):
        raw = random_filter(rng)
        f = parse_filter(raw, SCHEMA)
        sql, params = compile_sqlite(f)
        got = {r[0] for r in conn.execute(f"SELECT id FROM t WHERE {sql}", params)}
        want = {i for i, r in enumerate(rows) if matches(f, r)}
        assert got == want, (raw, sql, params)
        nonempty += bool(want) and len(want) < len(rows)
    assert nonempty > 100  # the generator produces selective filters, not just all/none


def test_sqlite_compiler_with_missing_fields() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, metadata TEXT NOT NULL)")
    conn.execute("INSERT INTO t VALUES (1, '{}')")
    for raw in ({"lang": {"$ne": "en"}}, {"lang": {"$nin": ["en"]}}, {"lang": "en"}, {"year": {"$gt": 1}}):
        f = parse_filter(raw, SCHEMA)
        sql, params = compile_sqlite(f)
        got = bool(conn.execute(f"SELECT id FROM t WHERE {sql}", params).fetchall())
        assert got == matches(f, {}), raw


def test_postgres_compiler_shape() -> None:
    f = parse_filter(
        {"lang": "en", "year": {"$gte": 2020}, "public": {"$ne": True}, "score": {"$in": [1, 0.5]}}, SCHEMA
    )
    sql, params = compile_postgres(f, SCHEMA)
    assert sql == (
        "(((metadata->>'lang')) = %s) AND (((metadata->>'year')::bigint) >= %s) AND "
        "(((metadata->>'public')::boolean) IS DISTINCT FROM %s) AND "
        "(((metadata->>'score')::double precision) IN (%s, %s))"
    )
    assert params == ["en", 2020, True, 1, 0.5]
    assert compile_postgres(None, SCHEMA) == ("TRUE", [])


def test_field_names_are_validated_before_interpolation() -> None:
    from ragcentre.stores.filter_sql import sqlite_json_field

    with pytest.raises(ValueError):
        sqlite_json_field()("x'); DROP TABLE t; --")


def test_check_field_rejects_trailing_newline() -> None:
    from ragcentre.stores.filter_sql import check_field

    assert check_field("lang") == "lang"
    with pytest.raises(ValueError):
        check_field("lang\n")
