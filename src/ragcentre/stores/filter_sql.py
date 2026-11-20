"""Compile filters (section 10.2) to parameterised SQL ``WHERE`` fragments.

SQLite: ``json_extract(metadata, '$.field')`` with booleans bound as 0/1.
PostgreSQL: ``(metadata->>'field')`` cast by the schema type, ``%s`` placeholders (psycopg).
Field names are interpolated only after passing ``FIELD_RE``; every value is a bound parameter.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from ragcentre.models.filters import And, Cond, FieldType, Filter, Or

FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

FieldExpr = Callable[[str], tuple[str, list[Any]]]

_SQL_OPS = {"$eq": "=", "$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}


def check_field(name: str) -> str:
    if not FIELD_RE.fullmatch(name):
        raise ValueError(f"invalid field name {name!r}")
    return name


def _bind(value: Any) -> Any:
    return int(value) if isinstance(value, bool) else value


def sqlite_json_field(column: str = "metadata") -> FieldExpr:
    check_field(column)

    def expr(field: str) -> tuple[str, list[Any]]:
        return f"json_extract({column}, '$.{check_field(field)}')", []

    return expr


def _same(value: Any) -> Any:
    return value


def _compile(
    f: Filter, field: FieldExpr, ph: str, ne_op: str, bind: Callable[[Any], Any] = _bind
) -> tuple[str, list[Any]]:
    if isinstance(f, (And, Or)):
        joiner = " AND " if isinstance(f, And) else " OR "
        parts: list[str] = []
        params: list[Any] = []
        for item in f.items:
            sql, p = _compile(item, field, ph, ne_op, bind)
            parts.append(f"({sql})")
            params.extend(p)
        return joiner.join(parts), params
    assert isinstance(f, Cond)
    col, col_params = field(f.field)
    if f.op in _SQL_OPS:
        return f"{col} {_SQL_OPS[f.op]} {ph}", [*col_params, bind(f.value)]
    if f.op == "$ne":
        return f"{col} {ne_op} {ph}", [*col_params, bind(f.value)]
    assert isinstance(f.value, tuple)
    marks = ", ".join(ph for _ in f.value)
    values = [bind(v) for v in f.value]
    if f.op == "$in":
        return f"{col} IN ({marks})", [*col_params, *values]
    # $nin: a missing value (NULL) is "not in" the list, as in matches().
    return f"({col} IS NULL OR {col} NOT IN ({marks}))", [*col_params, *col_params, *values]


def compile_sqlite(f: Filter | None, field: FieldExpr | None = None) -> tuple[str, list[Any]]:
    """Return ``(sql, params)``; ``("1", [])`` when there is no filter."""
    if f is None:
        return "1", []
    return _compile(f, field or sqlite_json_field(), "?", "IS NOT")


_PG_CASTS: dict[FieldType, str] = {"str": "", "int": "::bigint", "float": "::double precision", "bool": "::boolean"}


def postgres_json_field(schema: Mapping[str, FieldType], column: str = "metadata") -> FieldExpr:
    check_field(column)

    def expr(field: str) -> tuple[str, list[Any]]:
        cast = _PG_CASTS[schema.get(field, "str")]
        return f"(({column}->>'{check_field(field)}'){cast})", []

    return expr


def compile_postgres(
    f: Filter | None, schema: Mapping[str, FieldType], column: str = "metadata"
) -> tuple[str, list[Any]]:
    """PostgreSQL JSONB version; booleans are bound as Python bools (psycopg adapts them)."""
    if f is None:
        return "TRUE", []
    return _compile(f, postgres_json_field(schema, column), "%s", "IS DISTINCT FROM", _same)
