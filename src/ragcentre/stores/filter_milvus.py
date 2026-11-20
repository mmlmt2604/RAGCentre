"""Compile filters (section 10.2) to Milvus boolean expression strings.

Milvus filters are text, and ``MilvusClient`` has no portable way to bind parameters (expression
templates need a recent server), so this compiler is the injection boundary and is as strict as
``filter_sql``:

* Field names are interpolated only after passing ``FIELD_RE`` (letters, digits, underscore). They
  are used as JSON keys, ``metadata["name"]``, never as bare identifiers.
* Every value is rendered by ``string_literal`` / ``scalar_literal``. Strings are double-quoted;
  backslash, double quote, newline, carriage return and tab are backslash-escaped; every other
  character is emitted as is. Milvus Lite's parser has no ``\\u`` or ``\\x`` escapes, so control
  characters cannot be escaped portably, but inside a double-quoted literal they are inert.
  NUL, and text that cannot be encoded as UTF-8, are refused (``InvalidFilter``) rather than risk a
  truncated expression.
* Numbers are rendered from Python ``int`` / ``float`` only (never from text); integers outside the
  signed 64-bit range are refused; ``bool`` is rendered as ``true`` / ``false``.

Semantics match ``ragcentre.models.filters.matches`` for the JSON ``metadata`` column:

* a missing key never satisfies ``$eq``, ``$in`` or a range, and does satisfy ``$ne`` and ``$nin``
  (``!= x`` alone is false for a missing key in Milvus, so ``$ne`` / ``$nin`` add
  ``not exists(...)``);
* ``$in`` / ``$nin`` expand to ``||`` / ``&&`` of equality tests, because ``json["k"] in [...]`` is
  rejected by Milvus Lite (checked 2026-11-20, milvus-lite 3.2.2).

Every compound is fully parenthesised.
"""

from __future__ import annotations

from ragcentre.errors import InvalidFilter
from ragcentre.models.filters import And, Cond, Filter, Or, Scalar
from ragcentre.stores.filter_sql import check_field

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_RANGE_OPS = {"$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}


def string_literal(value: str) -> str:
    """A double-quoted Milvus string literal for ``value``."""
    if "\x00" in value:
        raise InvalidFilter("a NUL character cannot be used in a Milvus filter value")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidFilter("a filter value is not valid Unicode text") from exc
    return '"' + "".join(_ESCAPES.get(ch, ch) for ch in value) + '"'


def scalar_literal(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise InvalidFilter(f"integer {value} is outside the 64-bit range Milvus supports")
        return str(value)
    if isinstance(value, float):
        return repr(value)  # finite (parse_filter enforces it); repr round-trips, e.g. 1e-05
    return string_literal(value)


def json_key(field: str, column: str = "metadata") -> str:
    """``metadata["field"]`` after validating both names."""
    return f'{_checked(column)}["{_checked(field)}"]'


def _checked(field: str) -> str:
    try:
        return check_field(field)
    except ValueError as exc:
        raise InvalidFilter(str(exc)) from exc


def _equals(key: str, value: Scalar) -> str:
    return f"{key} == {scalar_literal(value)}"


def _compile(f: Filter, column: str) -> str:
    if isinstance(f, (And, Or)):
        joiner = " && " if isinstance(f, And) else " || "
        return "(" + joiner.join(_compile(item, column) for item in f.items) + ")"
    assert isinstance(f, Cond)
    key = json_key(f.field, column)
    if f.op == "$eq":
        assert not isinstance(f.value, tuple)
        return f"({_equals(key, f.value)})"
    if f.op in _RANGE_OPS:
        assert not isinstance(f.value, tuple)
        return f"({key} {_RANGE_OPS[f.op]} {scalar_literal(f.value)})"
    missing = f"not exists({key})"
    if f.op == "$ne":
        assert not isinstance(f.value, tuple)
        return f"({key} != {scalar_literal(f.value)} || {missing})"
    assert isinstance(f.value, tuple)
    if f.op == "$in":
        return "(" + " || ".join(_equals(key, v) for v in f.value) + ")"
    # $nin: a missing value is "not in" the list, as in matches().
    return f"({missing} || ({' && '.join(f'{key} != {scalar_literal(v)}' for v in f.value)}))"


def compile_milvus(f: Filter | None, column: str = "metadata") -> str:
    """The Milvus ``filter`` expression for ``f``; ``""`` (match everything) when there is no filter."""
    if f is None:
        return ""
    return _compile(f, column)
