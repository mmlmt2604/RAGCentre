"""Chunk enrichment: enrich nodes, the llm provider, caching, budgets and retrieval."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.enrichment import build_requests, stack
from ragcentre.errors import ExecutionError, InvalidPipeline
from ragcentre.pipeline.executors import execute
from ragcentre.pipeline.planner import create_pipeline
from ragcentre.queue.claims import claim_run, complete, fail
from ragcentre.testing.fakes import LLM_URL, FakeServices
from test_m6_ingestion import _add_providers, _corpus, _pipeline, _upload

DOC = b"# Warranty\n\nWarranty lasts twenty four months.\n\n## Claims\n\nClaims require a receipt."


def drain(ctx: AppContext) -> list[str]:
    failures: list[str] = []
    for _ in range(1000):
        run = claim_run(ctx, "test-drain")
        if run is None:
            return failures
        try:
            complete(ctx, run, execute(ctx, run))
        except ExecutionError as exc:
            failures.append(exc.code)
            fail(ctx, run, exc)
    raise AssertionError("queue did not drain")


def enriched(config: dict[str, Any] | None = None, **options: Any) -> dict[str, Any]:
    """The standard pipeline with dense search on enriched chunks and keyword search on plain ones."""
    cfg = copy.deepcopy(config or _pipeline())
    cfg["nodes"].insert(
        2, {"id": "enrich", "type": "enrich", "inputs": ["chunk"], "provider": "llm", "options": options}
    )
    for node in cfg["nodes"]:
        if node["id"] == "embed":
            node["inputs"] = ["enrich"]
    return cfg


@pytest.fixture
def setup(ctx: AppContext, client: TestClient) -> str:
    _add_providers(ctx)
    ctx.llm_provider_id = ctx.providers.create(
        "llm", "llm", "openai_compatible", {"base_url": LLM_URL, "model": "m"}
    ).id  # type: ignore[attr-defined]
    cid = _corpus(ctx)
    _upload(client, cid, DOC, external_id="manual")
    return cid


def test_enrich_pipeline_runs_and_embeds_enriched_text(
    ctx: AppContext, client: TestClient, fakes: FakeServices, setup: str
) -> None:
    pid = create_pipeline(ctx, setup, enriched())
    assert drain(ctx) == []
    rows = ctx.db.all("SELECT * FROM chunk_enrichments")
    assert rows and all(r["enrichment"].startswith("About:") for r in rows)
    from ragcentre.chunking.base import embedded_text
    from ragcentre.hashing import sha256_text

    chunk = ctx.db.one("SELECT c.*, e.enrichment FROM chunks c JOIN chunk_enrichments e ON e.chunk_id = c.id")
    sha = sha256_text(embedded_text(chunk["text"], chunk["context"], chunk["enrichment"]))
    assert ctx.cache.one("SELECT 1 FROM embeddings WHERE text_sha256 = ?", (sha,)) is not None
    body = client.post(f"/v1/corpora/{setup}/query", json={"pipeline_id": pid, "query": "warranty"}).json()
    assert body["enriched_sources"] == ["dense"]
    assert all(r["enrichment"].startswith("About:") for r in body["results"])


def test_llm_responses_are_cached_across_pipelines(ctx: AppContext, fakes: FakeServices, setup: str) -> None:
    create_pipeline(ctx, setup, enriched())
    drain(ctx)
    calls = len(fakes.llm_requests)
    assert calls > 0
    other = enriched()
    other["name"] = "again"
    other["nodes"][1]["options"] = {"max_chars": 1200, "overlap_chars": 0, "include_heading_path": False}
    other["nodes"][1]["options"]["include_heading_path"] = True  # same chunks, so same requests
    other["nodes"][2]["options"] = {"temperature": 0.0}
    create_pipeline(ctx, setup, other)
    drain(ctx)
    assert len(fakes.llm_requests) == calls


def test_prompt_change_changes_fingerprint_and_reuses_chunks(ctx: AppContext, setup: str) -> None:
    a = create_pipeline(ctx, setup, enriched())
    b_cfg = enriched(prompt="Say hi about {chunk}")
    b_cfg["name"] = "b"
    b = create_pipeline(ctx, setup, b_cfg)
    nodes = {
        pid: {
            r["node_id"]: r["node_fp"] for r in ctx.db.all("SELECT * FROM pipeline_nodes WHERE pipeline_id = ?", (pid,))
        }
        for pid in (a, b)
    }
    assert nodes[a]["chunk"] == nodes[b]["chunk"]
    assert nodes[a]["enrich"] != nodes[b]["enrich"]


def test_validation(ctx: AppContext, setup: str) -> None:
    bad = enriched(prompt="no placeholder")
    with pytest.raises(InvalidPipeline) as exc:
        create_pipeline(ctx, setup, bad)
    assert "{chunk}" in str(exc.value.details)
    wrong = enriched()
    wrong["nodes"][2]["provider"] = "hash"
    with pytest.raises(InvalidPipeline):
        create_pipeline(ctx, setup, wrong)


def test_two_enrich_nodes_feeding_sources_is_refused(ctx: AppContext, setup: str) -> None:
    cfg = enriched()
    cfg["nodes"].insert(3, {"id": "enrich2", "type": "enrich", "inputs": ["chunk"], "provider": "llm"})
    for node in cfg["nodes"]:
        if node["id"] == "fts":
            node["inputs"] = ["enrich2"]
    with pytest.raises(InvalidPipeline) as exc:
        create_pipeline(ctx, setup, cfg)
    assert "At most one enrich node" in str(exc.value.details)


def test_enrich_nodes_stack(ctx: AppContext, fakes: FakeServices, setup: str) -> None:
    cfg = enriched()
    cfg["nodes"].insert(
        3, {"id": "enrich2", "type": "enrich", "inputs": ["enrich"], "provider": "llm", "options": {"mode": "keywords"}}
    )
    for node in cfg["nodes"]:
        if node["id"] == "embed":
            node["inputs"] = ["enrich2"]
    create_pipeline(ctx, setup, cfg)
    assert drain(ctx) == []
    row = ctx.db.one("SELECT * FROM chunk_enrichments WHERE enrichment LIKE '%' || char(10) || char(10) || '%' LIMIT 1")
    assert row is not None and row["enrichment"].count("About:") == 2


def test_token_budget_fails_permanently(ctx: AppContext, fakes: FakeServices, setup: str) -> None:
    ctx.providers.update(ctx.llm_provider_id, {"max_tokens_per_run": 10})  # type: ignore[attr-defined]
    create_pipeline(ctx, setup, enriched())
    assert "llm_budget_exceeded" in drain(ctx)
    assert fakes.llm_requests == []


def test_embedded_text_and_helpers() -> None:
    from ragcentre.chunking.base import embedded_text

    assert embedded_text("t", None) == "t"
    assert embedded_text("t", "c") == "c\n\nt"
    assert embedded_text("t", "c", "e") == "e\n\nc\n\nt"
    assert stack(None, " a ") == "a"
    assert stack("x", "") == "x"
    [req] = build_requests(
        {"mode": "situate", "prompt": None, "max_tokens": 5, "temperature": 0.0},
        "{title}",
        "doc",
        [{"text": "{title}", "context": None, "heading_path": []}],
    )
    assert "<chunk>\n{title}\n</chunk>" in req.prompt  # values are not expanded twice
