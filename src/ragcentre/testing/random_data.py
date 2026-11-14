"""Seeded random metadata, filters and vectors for property tests and the store contract suite."""

from __future__ import annotations

import random
from typing import Any

from ragcentre.models.filters import FieldType

SCHEMA_FIELDS: dict[str, FieldType] = {"lang": "str", "year": "int", "score": "float", "public": "bool"}
LANGS = ["en", "de", "fr", "ja", "es"]
DOC_IDS = [f"doc-{i:02d}" for i in range(10)]


def full_schema() -> dict[str, FieldType]:
    return {**SCHEMA_FIELDS, "document_id": "str"}


def random_metadata(rng: random.Random) -> dict[str, Any]:
    return {
        "document_id": rng.choice(DOC_IDS),
        "lang": rng.choice(LANGS),
        "year": rng.randint(2015, 2025),
        "score": round(rng.uniform(0, 1), 2),
        "public": rng.random() < 0.5,
    }


def _value(rng: random.Random, field: str) -> Any:
    if field == "lang":
        return rng.choice([*LANGS, "zz"])
    if field == "document_id":
        return rng.choice(DOC_IDS)
    if field == "year":
        return rng.randint(2014, 2026)
    if field == "score":
        return rng.choice([round(rng.uniform(0, 1), 2), rng.randint(0, 1)])
    return rng.random() < 0.5


def random_condition(rng: random.Random, op: str | None = None, field: str | None = None) -> dict[str, Any]:
    schema = full_schema()
    if field is None:
        field = rng.choice(list(schema))
    ftype = schema[field]
    numeric = ftype in ("int", "float")
    if op is None:
        ops = ["$eq", "$ne", "$in", "$nin"] + (["$gt", "$gte", "$lt", "$lte"] if numeric else [])
        op = rng.choice(ops)
    if op in ("$in", "$nin"):
        value: Any = [_value(rng, field) for _ in range(rng.randint(1, 4))]
    else:
        value = _value(rng, field)
    if op == "$eq" and rng.random() < 0.5:
        return {field: value}  # shorthand
    return {field: {op: value}}


def random_filter(rng: random.Random, depth: int = 0) -> dict[str, Any]:
    r = rng.random()
    if depth >= 3 or r < 0.45:
        return random_condition(rng)
    if r < 0.6:
        # Several keys in one object: implicit $and.
        a, b = random_condition(rng), random_condition(rng)
        if set(a) & set(b):
            return {"$and": [a, b]}
        return {**a, **b}
    key = "$and" if r < 0.8 else "$or"
    return {key: [random_filter(rng, depth + 1) for _ in range(rng.randint(1, 3))]}


def random_vector(rng: random.Random, dim: int) -> list[float]:
    return [rng.uniform(-1, 1) for _ in range(dim)]
