"""Phase 2 of docs/plans/parser-compatibility.md: a parser describes its options, RAGCentre captures
the description when the provider is created and validates parse routes against it."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.pipeline.planner import plan
from ragcentre.providers.base import MAX_OPTIONS_SCHEMA_BYTES
from ragcentre.testing.fakes import PARSER_URL, FakeServices
from test_m3_providers_api import create
from test_m5_pipelines import _add_providers, _base_pipeline, _corpus, _expect_problem

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ocr": {"anyOf": [{"type": "boolean"}, {"const": "auto"}], "default": "auto", "description": "OCR mode."},
        "tier": {"enum": ["flash", "basic"], "type": "string", "description": "Tier."},
        "dpi": {"type": "integer", "minimum": 72, "maximum": 400, "description": "Resolution."},
        "tables": {"type": "boolean", "description": "Tables."},
        "engine": {"type": "object", "description": "Passthrough."},
    },
}


def parse_pipeline(options: dict[str, Any]) -> dict[str, Any]:
    cfg = _base_pipeline()
    cfg["nodes"][0]["routes"][1]["options"] = options
    return cfg


def problems_for(ctx: AppContext, options: dict[str, Any]) -> list[dict[str, Any]]:
    corpus_id = _corpus(ctx, f"c{len(ctx.db.all('SELECT id FROM corpora'))}")
    result: list[dict[str, Any]] = plan(ctx, corpus_id, parse_pipeline(options))["problems"]
    return result


@pytest.fixture
def with_schema(ctx: AppContext, fakes: FakeServices) -> AppContext:
    fakes.parser_options_schema = copy.deepcopy(SCHEMA)
    _add_providers(ctx)
    return ctx


def test_creation_captures_the_schema(client: TestClient, fakes: FakeServices) -> None:
    fakes.parser_options_schema = copy.deepcopy(SCHEMA)
    p = create(client, "docling", "parser", "http", {"base_url": PARSER_URL})
    assert p["config"]["options_schema"] == SCHEMA  # type: ignore[index]


def test_a_service_without_a_schema_stores_null(client: TestClient) -> None:
    p = create(client, "docling", "parser", "http", {"base_url": PARSER_URL})
    assert p["config"]["options_schema"] is None  # type: ignore[index]


@pytest.mark.parametrize(
    "bad",
    [
        "not an object",
        ["type", "object"],
        {"type": "nonsense"},
        {"type": "object", "properties": {"a": {"type": "boolean", "enum": 3}}},
        {"type": "object", "description": "x" * (MAX_OPTIONS_SCHEMA_BYTES + 1)},
    ],
)
def test_invalid_or_oversized_schema_fails_creation(client: TestClient, fakes: FakeServices, bad: Any) -> None:
    fakes.parser_options_schema = bad
    r = client.post(
        "/v1/providers",
        json={"name": "docling", "kind": "parser", "type": "http", "config": {"base_url": PARSER_URL}},
    )
    assert r.status_code == 502 and r.json()["error"]["code"] == "provider_error", r.text
    assert "options_schema" in r.json()["error"]["message"]
    assert client.get("/v1/providers", params={"limit": 50}).json()["items"] == [] or all(
        p["name"] != "docling" for p in client.get("/v1/providers", params={"limit": 50}).json()["items"]
    )


def test_the_schema_is_immutable(client: TestClient, fakes: FakeServices) -> None:
    fakes.parser_options_schema = copy.deepcopy(SCHEMA)
    p = create(client, "docling", "parser", "http", {"base_url": PARSER_URL})
    r = client.patch(f"/v1/providers/{p['id']}", json={"config": {"options_schema": {"type": "object"}}})
    assert r.status_code == 409
    # Unchanged values and operational changes still work, and the schema survives them.
    r = client.patch(f"/v1/providers/{p['id']}", json={"config": {"timeout_seconds": 30}})
    assert r.status_code == 200 and r.json()["config"]["options_schema"] == SCHEMA


def test_the_schema_is_not_part_of_the_fingerprint(client: TestClient, fakes: FakeServices) -> None:
    fakes.parser_options_schema = copy.deepcopy(SCHEMA)
    a = create(client, "docling-a", "parser", "http", {"base_url": PARSER_URL})
    fakes.parser_options_schema = {"type": "object", "properties": {"other": {"type": "string"}}}
    b = create(client, "docling-b", "parser", "http", {"base_url": PARSER_URL})
    none_schema = None
    fakes.parser_options_schema = none_schema
    c = create(client, "docling-c", "parser", "http", {"base_url": PARSER_URL})
    assert a["config"]["options_schema"] != b["config"]["options_schema"]
    assert a["fingerprint"] == b["fingerprint"] == c["fingerprint"]


def test_valid_options_pass(with_schema: AppContext) -> None:
    ok = {"ocr": "auto", "tier": "basic", "dpi": 150, "tables": False, "engine": {"x": [1, 2]}}
    assert problems_for(with_schema, ok) == []
    assert problems_for(with_schema, {}) == []
    assert problems_for(with_schema, {"ocr": True}) == []


def test_unknown_option_is_a_problem_at_its_key(with_schema: AppContext) -> None:
    found = problems_for(with_schema, {"ocrr": True})
    _expect_problem(found, "nodes[0].routes[1].options.ocrr", "parse")
    assert "no such option" in next(p["message"] for p in found if p["path"].endswith(".ocrr"))


@pytest.mark.parametrize(
    ("options", "key", "text"),
    [
        ({"tables": "false"}, "tables", "true or false"),
        ({"dpi": "150"}, "dpi", "whole number"),
        ({"dpi": 401}, "dpi", "at most 400"),
        ({"dpi": 71}, "dpi", "at least 72"),
        ({"tier": "fast"}, "tier", '"flash", "basic"'),
        ({"ocr": "yes"}, "ocr", 'true or false or "auto"'),
        ({"engine": "x"}, "engine", "an object"),
    ],
)
def test_wrong_values_are_problems_at_the_key(
    with_schema: AppContext, options: dict[str, Any], key: str, text: str
) -> None:
    found = problems_for(with_schema, options)
    _expect_problem(found, f"nodes[0].routes[1].options.{key}", "parse")
    assert text in next(p["message"] for p in found if p["path"].endswith(f".{key}"))


def test_every_error_is_reported(with_schema: AppContext) -> None:
    found = problems_for(with_schema, {"dpi": 1, "tables": 3, "zzz": 1})
    assert {p["path"].rsplit(".", 1)[1] for p in found} == {"dpi", "tables", "zzz"}


def test_a_provider_without_a_schema_is_not_validated(ctx: AppContext, fakes: FakeServices) -> None:
    _add_providers(ctx)  # the fake reports no schema
    assert problems_for(ctx, {"anything": {"goes": 1}}) == []


def test_creating_a_pipeline_with_a_mistyped_option_is_422(with_schema: AppContext, client: TestClient) -> None:
    corpus_id = _corpus(with_schema)
    r = client.post(f"/v1/corpora/{corpus_id}/pipelines", json=parse_pipeline({"ocrr": True}))
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_pipeline"
    _expect_problem(r.json()["error"]["details"]["problems"], "nodes[0].routes[1].options.ocrr")
    ok = client.post(f"/v1/corpora/{corpus_id}/pipelines", json=parse_pipeline({"ocr": False}))
    assert ok.status_code in (200, 201), ok.text


def test_a_nullable_union_is_described_in_one_sentence(ctx: AppContext, fakes: FakeServices) -> None:
    fakes.parser_options_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "page_range": {"anyOf": [{"type": "array", "items": {"type": "integer"}, "maxItems": 2}, {"type": "null"}]}
        },
    }
    _add_providers(ctx)
    found = problems_for(ctx, {"page_range": [1, 2, 3]})
    assert [p["message"] for p in found] == ["Must have at most 2 items or be null."]
    assert [p["message"] for p in problems_for(ctx, {"page_range": "x"})] == ["Must be a list or null."]
