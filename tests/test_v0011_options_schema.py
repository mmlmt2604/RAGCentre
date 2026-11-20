"""Typed parser options (C1): a service publishes ``options_schema``; RAGCentre keeps it with the
provider and validates a pipeline's parse routes against it."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.pipeline.planner import resolve_for_corpus
from ragcentre.providers.parsers.options import validate_route_options
from ragcentre.testing.fakes import PARSER_URL, FakeServices
from test_m5_pipelines import _add_providers, _base_pipeline, _corpus

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ocr": {"type": "boolean", "default": True},
        "tables": {"type": "string", "enum": ["fast", "accurate"]},
        "dpi": {"type": "integer", "minimum": 72, "maximum": 400},
        "scale": {"type": "number", "minimum": 0.5},
        "page_range": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2, "maxItems": 2},
        "lang": {"type": "array", "items": {"type": "string"}},
        "ocr_mode": {"enum": [True, False, "auto"]},
    },
}


@pytest.mark.parametrize(
    ("options", "key", "message"),
    [
        ({"tabels": False}, "tabels", 'Unknown option "tabels"'),
        ({"ocr": "no"}, "ocr", "Must be true or false"),
        ({"tables": "slow"}, "tables", "Must be one of: fast, accurate"),
        ({"dpi": 10}, "dpi", "Must be at least 72"),
        ({"dpi": 500}, "dpi", "Must be at most 400"),
        ({"dpi": 100.5}, "dpi", "Must be an integer"),
        ({"dpi": True}, "dpi", "Must be an integer"),
        ({"scale": 0.1}, "scale", "Must be at least 0.5"),
        ({"page_range": [1]}, "page_range", "Needs at least 2 items"),
        ({"page_range": [0, 3]}, "page_range", "Each item: must be at least 1"),
        ({"lang": "en"}, "lang", "Must be a list"),
        ({"ocr_mode": "maybe"}, "ocr_mode", "Must be one of: true, false, auto"),
        ({"ocr_mode": 1}, "ocr_mode", "Must be one of"),
    ],
)
def test_route_options_problems(options: dict[str, Any], key: str, message: str) -> None:
    problems = validate_route_options(SCHEMA, options)
    assert [(k, m) for k, m in problems if k == key], problems
    assert message in dict(problems)[key]


def test_valid_options_and_leniency() -> None:
    ok = {
        "ocr": False,
        "tables": "fast",
        "dpi": 200,
        "scale": 1,
        "page_range": [1, 5],
        "lang": ["en"],
        "ocr_mode": "auto",
    }
    assert validate_route_options(SCHEMA, ok) == []
    assert validate_route_options(SCHEMA, {}) == []
    # A schema without additionalProperties: false lets unknown keys through; odd schemas are ignored.
    assert validate_route_options({"properties": {"a": {"type": "boolean"}}}, {"zzz": 1}) == []
    for odd in (None, [], "x", {"properties": []}, {"properties": {"a": "nope"}}):
        assert validate_route_options(odd, {"a": 1}) == []
    assert validate_route_options(SCHEMA, "not a dict") == []  # type: ignore[arg-type]


def parse_pipeline(options: dict[str, Any]) -> dict[str, Any]:
    cfg = _base_pipeline()
    cfg["nodes"][0]["routes"] = [{"mime": "*/*", "provider": "docling", "options": options}]
    return cfg


def test_provider_keeps_the_published_schema(ctx: AppContext, fakes: FakeServices) -> None:
    fakes.parser_options_schema = SCHEMA
    row = ctx.providers.create("docling", "parser", "http", {"base_url": PARSER_URL})
    assert row.config["options_schema"] == SCHEMA
    # It is captured, not editable, and not part of the fingerprint.
    fingerprint = row.fingerprint
    fakes.parser_options_schema = {**SCHEMA, "properties": {}}
    from ragcentre.errors import ImmutableField

    with pytest.raises(ImmutableField):
        ctx.providers.update(row.id, {"options_schema": {}})
    ctx.providers.update(row.id, {"timeout_seconds": 30})
    assert ctx.providers.get(row.id).config["options_schema"] == SCHEMA
    assert ctx.providers.get(row.id).fingerprint == fingerprint


def test_service_without_a_schema_has_none(ctx: AppContext) -> None:
    row = ctx.providers.create("docling", "parser", "http", {"base_url": PARSER_URL})
    assert "options_schema" not in row.config


def test_pipeline_validation_uses_the_schema(ctx: AppContext, fakes: FakeServices) -> None:
    fakes.parser_options_schema = SCHEMA
    _add_providers(ctx)  # creates "docling" against the fake parser
    ctx.providers.delete(ctx.providers.by_name("docling").id)
    ctx.providers.create("docling", "parser", "http", {"base_url": PARSER_URL})
    corpus_id = _corpus(ctx)

    def problems(options: dict[str, Any]) -> list[dict[str, Any]]:
        return [p.to_json() for p in resolve_for_corpus(ctx, corpus_id, parse_pipeline(options)).problems]

    assert problems({"ocr": False, "dpi": 150}) == []
    found = problems({"tabels": False})
    assert [(p["path"], p["node_id"]) for p in found] == [("nodes[0].routes[0].options.tabels", "parse")]
    assert "Unknown option" in found[0]["message"] and "ocr, tables" not in found[0]["message"]
    assert [p["path"] for p in problems({"dpi": 5, "ocr": "yes"})] == [
        "nodes[0].routes[0].options.dpi",
        "nodes[0].routes[0].options.ocr",
    ]


def test_a_service_without_a_schema_accepts_any_options(ctx: AppContext) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    res = resolve_for_corpus(ctx, corpus_id, parse_pipeline({"anything": [1, 2]}))
    assert res.problems == []


def test_deep_test_refreshes_the_schema(ctx: AppContext, client: TestClient, fakes: FakeServices) -> None:
    row = ctx.providers.create("docling", "parser", "http", {"base_url": PARSER_URL})
    assert "options_schema" not in row.config
    fakes.parser_options_schema = SCHEMA  # the service gained options without a version change
    assert client.post(f"/v1/providers/{row.id}/test").json()["ok"] is True
    assert ctx.providers.get(row.id).config["options_schema"] == SCHEMA
    # and the API shows it to the UI
    shown = client.get(f"/v1/providers/{row.id}").json()
    assert shown["config"]["options_schema"] == SCHEMA
