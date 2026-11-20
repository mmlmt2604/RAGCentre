"""Milvus filter compiler tests: pure string building, no pymilvus and no server needed."""

from __future__ import annotations

from typing import Any

import pytest

from ragcentre.errors import InvalidFilter
from ragcentre.models.filters import And, Cond, Or, parse_filter
from ragcentre.stores.filter_milvus import INT64_MAX, INT64_MIN, compile_milvus, scalar_literal, string_literal

SCHEMA: dict[str, Any] = {"lang": "str", "year": "int", "score": "float", "public": "bool", "document_id": "str"}


# ---- filter compiler (no pymilvus needed) ---------------------------------------------------------


def _c(raw: Any) -> str:
    return compile_milvus(parse_filter(raw, SCHEMA))


def test_no_filter_is_an_empty_expression() -> None:
    assert compile_milvus(None) == ""


def test_operators_compile_to_parenthesised_expressions() -> None:
    assert _c({"lang": "en"}) == '(metadata["lang"] == "en")'
    assert _c({"year": {"$gte": 2020}}) == '(metadata["year"] >= 2020)'
    assert _c({"score": {"$lt": 0.5}}) == '(metadata["score"] < 0.5)'
    assert _c({"public": True}) == '(metadata["public"] == true)'
    # $ne and $nin hold for a missing key, as in the reference evaluator
    assert _c({"lang": {"$ne": "de"}}) == '(metadata["lang"] != "de" || not exists(metadata["lang"]))'
    assert _c({"lang": {"$in": ["en", "de"]}}) == '(metadata["lang"] == "en" || metadata["lang"] == "de")'
    assert _c({"lang": {"$nin": ["en", "de"]}}) == (
        '(not exists(metadata["lang"]) || (metadata["lang"] != "en" && metadata["lang"] != "de"))'
    )
    assert _c({"$and": [{"lang": "en"}, {"year": 1}]}) == '((metadata["lang"] == "en") && (metadata["year"] == 1))'
    assert _c({"$or": [{"lang": "en"}, {"year": 1}]}) == '((metadata["lang"] == "en") || (metadata["year"] == 1))'


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        ("plain", '"plain"'),
        ('quo"te', '"quo\\"te"'),
        ("back\\slash", '"back\\\\slash"'),
        ('a\\"b', '"a\\\\\\"b"'),
        ("new\nline", '"new\\nline"'),
        ("cr\rx", '"cr\\rx"'),
        ("tab\tx", '"tab\\tx"'),
        ("single'quote", '"single\'quote"'),
        ("é中\U0001f600", '"é中\U0001f600"'),
    ],
)
def test_string_literals(value: str, literal: str) -> None:
    assert string_literal(value) == literal


def test_string_literal_refuses_nul_and_invalid_unicode() -> None:
    with pytest.raises(InvalidFilter):
        string_literal("a\x00b")
    with pytest.raises(InvalidFilter):
        string_literal("lone \ud800 surrogate")


def test_injection_payloads_stay_inside_one_literal() -> None:
    for payload in ['" || true || "', '"] || (true) && ["', '\\" || 1==1 //', "x\\", '\\"', '\\\\"']:
        expr = compile_milvus(Cond("lang", "$eq", payload))
        assert expr.startswith('(metadata["lang"] == "') and expr.endswith('")')
        inner = expr[len('(metadata["lang"] == "') : -2]
        # every quote inside the literal is escaped by an odd run of backslashes
        i = 0
        while i < len(inner):
            if inner[i] == "\\":
                i += 2
                continue
            assert inner[i] != '"', (payload, expr)
            i += 1


def test_scalar_literals() -> None:
    assert scalar_literal(True) == "true" and scalar_literal(False) == "false"
    assert scalar_literal(7) == "7" and scalar_literal(-3) == "-3"
    assert scalar_literal(0.5) == "0.5" and scalar_literal(1e-5) == "1e-05"
    assert scalar_literal(INT64_MAX) == str(INT64_MAX) and scalar_literal(INT64_MIN) == str(INT64_MIN)
    for out_of_range in (INT64_MAX + 1, INT64_MIN - 1):
        with pytest.raises(InvalidFilter):
            scalar_literal(out_of_range)


def test_field_names_are_validated_before_interpolation() -> None:
    for bad in ('x"]) || (true', "a b", "a-b", "", 'a"', "a]"):
        with pytest.raises(InvalidFilter):
            compile_milvus(Cond(bad, "$eq", 1))
    with pytest.raises(InvalidFilter):
        compile_milvus(Cond("lang", "$eq", "x"), column='m"; drop')


def test_compiler_never_emits_user_text_outside_literals() -> None:
    f = And((Or((Cond("lang", "$in", ("a", 'b"c')), Cond("year", "$gt", 3))), Cond("public", "$ne", True)))
    out = compile_milvus(f)
    assert out.count("(") == out.count(")")
    assert "&&" in out and "||" in out
