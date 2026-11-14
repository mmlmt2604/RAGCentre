"""The filter language (section 10.2): parse and validate, and a pure-Python evaluator.

``parse_filter`` turns the Mongo-style JSON into a normalised tree of ``Cond``, ``And`` and
``Or`` nodes. ``matches`` is the reference evaluator and the ground truth for adapter tests.
Compilers to SQL live in ``ragcentre.stores.filter_sql``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from ragcentre.errors import InvalidFilter

FieldType = Literal["str", "int", "float", "bool"]
Scalar = str | int | float | bool
Op = Literal["$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin"]

OPS: frozenset[str] = frozenset({"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin"})
RANGE_OPS: frozenset[str] = frozenset({"$gt", "$gte", "$lt", "$lte"})
LIST_OPS: frozenset[str] = frozenset({"$in", "$nin"})
MAX_LIST = 100
MAX_DEPTH = 5


@dataclass(frozen=True)
class Cond:
    field: str
    op: Op
    value: Scalar | tuple[Scalar, ...]


@dataclass(frozen=True)
class And:
    items: tuple[Filter, ...]


@dataclass(frozen=True)
class Or:
    items: tuple[Filter, ...]


Filter = Cond | And | Or


def schema_types(metadata_schema: Mapping[str, Any]) -> dict[str, FieldType]:
    """Field types from a corpus ``metadata_schema``, plus ``document_id``."""
    fields = metadata_schema.get("fields", {}) if "fields" in metadata_schema else metadata_schema
    out: dict[str, FieldType] = {name: spec["type"] for name, spec in fields.items()}
    out["document_id"] = "str"
    return out


def type_ok(value: Any, ftype: FieldType) -> bool:
    if ftype == "str":
        return isinstance(value, str)
    if ftype == "bool":
        return isinstance(value, bool)
    if ftype == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if ftype == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return False  # pragma: no cover


class _Parser:
    def __init__(self, schema: Mapping[str, FieldType]) -> None:
        self.schema = schema

    def fail(self, path: str, message: str) -> InvalidFilter:
        return InvalidFilter(f"{path}: {message}" if path else message, details={"path": path})

    def node(self, raw: Any, path: str, depth: int) -> Filter:
        if depth > MAX_DEPTH:
            raise self.fail(path, f"filters may nest $and/$or at most {MAX_DEPTH} deep")
        if not isinstance(raw, dict):
            raise self.fail(path, "a filter must be a JSON object")
        if not raw:
            raise self.fail(path, "a filter object must not be empty")
        parts: list[Filter] = []
        for key, value in raw.items():
            sub = f"{path}.{key}" if path else key
            if key in ("$and", "$or"):
                if not isinstance(value, list) or not value:
                    raise self.fail(sub, f"{key} takes a non-empty list of filters")
                items = tuple(self.node(v, f"{sub}[{i}]", depth + 1) for i, v in enumerate(value))
                parts.append(_combine(And if key == "$and" else Or, items))
            elif key.startswith("$"):
                raise self.fail(sub, f"unknown operator {key}")
            else:
                parts.extend(self.field(key, value, sub))
        return _combine(And, tuple(parts))

    def field(self, name: str, raw: Any, path: str) -> list[Filter]:
        ftype = self.schema.get(name)
        if ftype is None:
            known = ", ".join(sorted(self.schema))
            raise self.fail(path, f"unknown field {name!r}; filterable fields are: {known}")
        if not isinstance(raw, dict):
            return [self.cond(name, ftype, "$eq", raw, path)]
        if not raw:
            raise self.fail(path, "an operator object must not be empty")
        return [self.cond(name, ftype, op, value, f"{path}.{op}") for op, value in raw.items()]

    def cond(self, name: str, ftype: FieldType, op: str, value: Any, path: str) -> Cond:
        if op not in OPS:
            raise self.fail(path, f"unknown operator {op}")
        if op in RANGE_OPS and ftype not in ("int", "float"):
            raise self.fail(path, f"{op} applies to int and float fields only; {name} is {ftype}")
        if op in LIST_OPS:
            if not isinstance(value, list) or not value:
                raise self.fail(path, f"{op} takes a non-empty list")
            if len(value) > MAX_LIST:
                raise self.fail(path, f"{op} takes at most {MAX_LIST} values")
            for i, v in enumerate(value):
                if not type_ok(v, ftype):
                    raise self.fail(f"{path}[{i}]", f"expected a {ftype} value for {name}")
            return Cond(name, op, tuple(value))  # type: ignore[arg-type]
        if not type_ok(value, ftype):
            raise self.fail(path, f"expected a {ftype} value for {name}, got {type(value).__name__}")
        return Cond(name, op, value)  # type: ignore[arg-type]


def _combine(kind: type[And] | type[Or], items: tuple[Filter, ...]) -> Filter:
    flat: list[Filter] = []
    for item in items:
        if isinstance(item, kind):
            flat.extend(item.items)
        else:
            flat.append(item)
    if len(flat) == 1:
        return flat[0]
    return kind(tuple(flat))


def parse_filter(raw: Any, schema: Mapping[str, FieldType]) -> Filter | None:
    """Validate and normalise a filter. ``None`` or ``{}`` means no filter."""
    if raw is None or raw == {}:
        return None
    return _Parser(schema).node(raw, "", 1)


def to_json(f: Filter) -> dict[str, Any]:
    """Back to the explicit JSON form (for storage and adapters that speak the language)."""
    if isinstance(f, Cond):
        value = list(f.value) if isinstance(f.value, tuple) else f.value
        return {f.field: {f.op: value}}
    key = "$and" if isinstance(f, And) else "$or"
    return {key: [to_json(i) for i in f.items]}


def _eq(actual: Any, expected: Any) -> bool:
    if actual is None:
        return False
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    return bool(actual == expected)


def _cmp(op: str, actual: Any, expected: Any) -> bool:
    if op == "$eq":
        return _eq(actual, expected)
    if op == "$ne":
        return not _eq(actual, expected)
    if actual is None or isinstance(actual, (bool, str)):
        return False
    if op == "$gt":
        return bool(actual > expected)
    if op == "$gte":
        return bool(actual >= expected)
    if op == "$lt":
        return bool(actual < expected)
    if op == "$lte":
        return bool(actual <= expected)
    raise ValueError(op)  # pragma: no cover


def matches(f: Filter | None, metadata: Mapping[str, Any]) -> bool:
    """Pure-Python reference evaluator. A missing field compares as absent (only $ne/$nin hold)."""
    if f is None:
        return True
    if isinstance(f, And):
        return all(matches(i, metadata) for i in f.items)
    if isinstance(f, Or):
        return any(matches(i, metadata) for i in f.items)
    actual = metadata.get(f.field)
    if f.op == "$in":
        assert isinstance(f.value, tuple)
        return any(_eq(actual, v) for v in f.value)
    if f.op == "$nin":
        assert isinstance(f.value, tuple)
        return not any(_eq(actual, v) for v in f.value)
    return _cmp(f.op, actual, f.value)


def fields_used(f: Filter | None) -> set[str]:
    if f is None:
        return set()
    if isinstance(f, Cond):
        return {f.field}
    out: set[str] = set()
    for i in f.items:
        out |= fields_used(i)
    return out
