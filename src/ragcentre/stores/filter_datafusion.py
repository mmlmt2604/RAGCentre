"""Compile filters (section 10.2) to DataFusion SQL ``WHERE`` strings, for LanceDB.

LanceDB filters are SQL text, not bound parameters, so this compiler is the injection boundary and
is as strict as ``filter_sql``:

* Field names are interpolated only after fully matching ``FIELD_RE`` (the same pattern the
  metadata schema enforces), optionally against an allow-list of the collection's known fields, and are
  always quoted with backticks (LanceDB resolves columns case-sensitively only when quoted, and
  quoting also keeps SQL keywords such as ``order`` usable as field names).
* String values become single-quoted literals with ``'`` doubled. Standard SQL has no other
  escape inside a literal (a backslash is an ordinary character in DataFusion), so nothing else
  can end the literal. Lone surrogates, which cannot be encoded to UTF-8 for the engine, are
  rejected.
* Numbers are rendered from Python ``int``/``float`` values (never from text): integers must fit
  in 64 bits, floats must be finite. Booleans become ``true``/``false``.

Semantics follow ``ragcentre.models.filters.matches``: a missing (``NULL``) field satisfies only
``$ne`` and ``$nin``. SQL's three-valued logic would drop NULL rows from ``<>`` and ``NOT IN``,
so those two compile to ``(col IS NULL OR ...)``. Rows gain NULLs when a field is added to an
existing collection.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from ragcentre.models.filters import And, Cond, Filter, Or
from ragcentre.stores.filter_sql import FIELD_RE

TRUE = "true"
_SQL_OPS = {"$eq": "=", "$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def quote_ident(name: str, allowed: Collection[str] | None = None) -> str:
    """A validated, backtick-quoted column name."""
    # fullmatch, not match: FIELD_RE ends in ``$``, which also accepts a trailing newline.
    if not FIELD_RE.fullmatch(name):
        raise ValueError(f"invalid field name {name!r}")
    if allowed is not None and name not in allowed:
        raise ValueError(f"unknown field {name!r}")
    return f"`{name}`"


def string_literal(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("string values must be valid Unicode (no lone surrogates)") from exc
    return "'" + value.replace("'", "''") + "'"


def literal(value: Any) -> str:
    """A SQL literal for a filter value (``str``, ``bool``, ``int`` or finite ``float``)."""
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, str):
        return string_literal(value)
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise ValueError(f"integer {value} does not fit in 64 bits")
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("float values must be finite")
        return repr(value)
    raise ValueError(f"unsupported filter value type {type(value).__name__}")


def compile_datafusion(f: Filter | None, allowed: Collection[str] | None = None) -> str:
    """Return the ``WHERE`` text for ``f`` (``"true"`` when there is no filter).

    ``allowed``, when given, is the set of field names the collection has; any other field raises
    ``ValueError``. Raises ``ValueError`` for an invalid field name or value.
    """
    if f is None:
        return TRUE
    return _compile(f, allowed)


def _compile(f: Filter, allowed: Collection[str] | None) -> str:
    if isinstance(f, (And, Or)):
        joiner = " AND " if isinstance(f, And) else " OR "
        return joiner.join(f"({_compile(item, allowed)})" for item in f.items)
    assert isinstance(f, Cond)
    col = quote_ident(f.field, allowed)
    if f.op in _SQL_OPS:
        assert not isinstance(f.value, tuple)
        return f"{col} {_SQL_OPS[f.op]} {literal(f.value)}"
    if f.op == "$ne":
        assert not isinstance(f.value, tuple)
        return f"({col} IS NULL OR {col} <> {literal(f.value)})"
    assert isinstance(f.value, tuple)
    values = ", ".join(literal(v) for v in f.value)
    if f.op == "$in":
        return f"{col} IN ({values})"
    return f"({col} IS NULL OR {col} NOT IN ({values}))"
