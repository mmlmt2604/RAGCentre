"""Retrieval acceptance tests against real ingestion and sqlite-vec/FTS."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.pipeline.executors import execute
from ragcentre.pipeline.planner import create_pipeline
from ragcentre.queue.claims import claim_run, complete, fail
from ragcentre.retrieval.fusion import SourceMatch, rrf, weighted
from ragcentre.testing.fakes import RERANKER_URL, FakeServices
from test_m6_ingestion import _add_providers, _corpus, _pipeline, _upload


def drain(ctx: AppContext) -> None:
    for _ in range(1000):
        run = claim_run(ctx, "test-drain")
        if run is None:
            return
        complete(ctx, run, execute(ctx, run))
    raise AssertionError("queue did not drain")


@pytest.fixture
def ready(ctx: AppContext, client: TestClient) -> tuple[str, str]:
    _add_providers(ctx)
    cid = _corpus(ctx)
    ctx.db.execute(
        "UPDATE corpora SET metadata_schema = ? WHERE id = ?",
        (json.dumps({"fields": {"lang": {"type": "str", "default": "en"}}}), cid),
    )
    _upload(
        client,
        cid,
        b"# Warranty\n\nWarranty lasts twenty four months.\n\n## Claims\n\nWarranty claims require a receipt.",
        external_id="manual",
    )
    _upload(client, cid, b"# Warranty\n\nWarranty lasts twelve months.", metadata={"lang": "de"}, external_id="other")
    config = _pipeline()
    config["retrieval"]["expand"] = {"neighbors": 1}
    pid = create_pipeline(ctx, cid, config)
    drain(ctx)
    return cid, pid


def ask(client: TestClient, ready: tuple[str, str], **kwargs: object) -> httpx.Response:
    cid, pid = ready
    return client.post(f"/v1/corpora/{cid}/query", json={"pipeline_id": pid, "query": "warranty", **kwargs})


def test_fusion_hand_calculation() -> None:
    sources = {
        "dense": [SourceMatch("dense", "a", 0.9, 1), SourceMatch("dense", "b", 0.3, 2)],
        "sparse": [SourceMatch("sparse", "b", 7, 1), SourceMatch("sparse", "c", 7, 2)],
    }
    assert dict(rrf(sources, {"dense": 2, "sparse": 1}, 60)) == pytest.approx(
        {"a": 2 / 61, "b": 2 / 62 + 1 / 61, "c": 1 / 62}
    )
    assert weighted(sources, {"dense": 2, "sparse": 1}) == [("a", 2), ("b", 1), ("c", 1)]


def test_query_shapes_sources_expansion(client: TestClient, ready: tuple[str, str]) -> None:
    response = ask(client, ready)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["partial"] is False
    assert body["results"]
    assert set(body) == {
        "pipeline_id",
        "pipeline_fingerprint",
        "partial",
        "enriched_sources",
        "results",
        "timings_ms",
        "transform",
    }
    assert body["transform"] is None
    assert "total" in body["timings_ms"]
    assert any(r["context_before"] or r["context_after"] for r in body["results"])
    assert all(set(r["sources"]) == {"dense", "sparse"} for r in body["results"])


@pytest.mark.parametrize("text", ['"', "*", "NEAR", "-", '" * NEAR - warranty', ""])
def test_fts_operators_safe(client: TestClient, ready: tuple[str, str], text: str) -> None:
    response = ask(client, ready, query=text)
    assert response.status_code == 200, response.text


def test_filter_defaults_both_sources(client: TestClient, ready: tuple[str, str]) -> None:
    for lang in ("en", "de"):
        response = ask(client, ready, filter={"lang": lang})
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert results
        assert all(r["document"]["metadata"].get("lang", "en") == lang for r in results)
        assert all(set(r["sources"]) == {"dense", "sparse"} for r in results)
    assert ask(client, ready, filter={"unknown": 1}).json()["error"]["code"] == "invalid_filter"


def test_query_applies_current_metadata_before_vector_refresh(
    ctx: AppContext, client: TestClient, ready: tuple[str, str]
) -> None:
    document_id = ctx.db.scalar("SELECT id FROM documents WHERE external_id = 'manual'")
    assert client.patch(f"/v1/documents/{document_id}", json={"metadata": {"lang": "de"}}).is_success
    filtered = ask(client, ready, filter={"lang": "en"}, allow_partial=True)
    assert filtered.is_success
    assert filtered.json()["results"] == []
    assert filtered.json()["partial"] is True


@pytest.mark.parametrize(
    "condition,code",
    [
        ("missing", "index_not_built"),
        ("archive", "pipeline_archived"),
        ("incomplete", "index_incomplete"),
        ("collection", "index_mismatch"),
        ("metric", "index_mismatch"),
        ("dimension", "embedding_dimension_mismatch"),
        ("fts", "index_mismatch"),
    ],
)
def test_guards(
    ctx: AppContext,
    client: TestClient,
    ready: tuple[str, str],
    condition: str,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid, pid = ready
    index = ctx.db.one("SELECT * FROM indexes WHERE corpus_id = ? AND kind = 'vector'", (cid,))
    assert index is not None
    if condition == "missing":
        ctx.db.execute("DELETE FROM indexes WHERE id = ?", (index["id"],))
    elif condition == "archive":
        ctx.db.execute("UPDATE pipelines SET archived = 1 WHERE id = ?", (pid,))
    elif condition == "incomplete":
        ctx.db.execute("UPDATE node_runs SET status = 'pending'")
    elif condition == "collection":
        ctx.providers.store(index["provider_id"]).drop_collection(index["physical_name"])
    elif condition == "metric":
        ctx.db.execute("UPDATE indexes SET metric = 'l2' WHERE id = ?", (index["id"],))
    elif condition == "fts":
        name = ctx.db.scalar("SELECT physical_name FROM indexes WHERE kind = 'fts'")
        ctx.db.execute(f'DROP TABLE "fts_{name}"')
    else:
        from ragcentre.embedding_service import EmbedResult

        monkeypatch.setattr(ctx.embeddings, "embed", lambda *a: EmbedResult([[1.0, 0.0]]))
    response = ask(client, ready)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == code
    if condition == "incomplete":
        assert ask(client, ready, allow_partial=True).json()["partial"] is True


def test_version_drift_node_failure(ctx: AppContext, client: TestClient, fakes: FakeServices) -> None:
    _add_providers(ctx, http_parser=True)
    cid = _corpus(ctx)
    _upload(client, cid, b"new document")
    create_pipeline(ctx, cid, _pipeline(http_parser=True))
    fakes.parser_version = "99"
    run = claim_run(ctx, "drift")
    assert run is not None
    from ragcentre.errors import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        execute(ctx, run)
    assert exc.value.code == "provider_version_drift" and not exc.value.retryable
    fail(ctx, run, exc.value)
    assert ctx.db.scalar("SELECT status FROM node_runs WHERE id = ?", (run.id,)) == "failed"


def test_rerank_failure_is_502_and_no_retry(
    ctx: AppContext, client: TestClient, ready: tuple[str, str], fakes: FakeServices
) -> None:
    cid, _ = ready
    ctx.providers.create("ranker", "reranker", "http", {"base_url": RERANKER_URL, "model": "test"})
    cfg = _pipeline()
    cfg["name"] = "reranked"
    cfg["retrieval"]["rerank"] = {"provider": "ranker", "top_n": 30}
    pid = create_pipeline(ctx, cid, cfg)
    fakes.script("reranker", "/rerank", [503])
    response = ask(client, (cid, pid))
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "provider_error"
    assert fakes.count("reranker", "/rerank") == 1
    assert ask(client, (cid, pid)).status_code == 200
