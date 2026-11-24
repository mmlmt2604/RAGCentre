"""A pipeline without a transform returns exactly what it returned before query transforms.

The golden file was recorded with the engine as it was before probes existed. Set
``UPDATE_GOLDEN=1`` to rewrite it after an intended change to retrieval results.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.pipeline.planner import create_pipeline
from ragcentre.testing.fakes import RERANKER_URL
from test_m6_ingestion import _add_providers, _corpus, _pipeline, _upload
from test_m7_retrieval import drain

GOLDEN = Path(__file__).parent / "golden" / "query_single_probe.json"

DOCS = {
    "manual": b"# Warranty\n\nWarranty lasts twenty four months.\n\n## Claims\n\nWarranty claims require a receipt.\n\n"
    b"## Shipping\n\nParcels ship within three days and arrive by courier.",
    "other": b"# Warranty\n\nWarranty lasts twelve months for refurbished devices.",
    "faq": b"# Returns\n\nReturns are accepted within thirty days with a receipt.\n\n## Refunds\n\nRefunds go back "
    b"to the original payment method.",
}
QUERIES = ["warranty", "warranty claims receipt", "twelve months", "refund payment", "zzz unknown"]


def _variants() -> dict[str, dict[str, Any]]:
    def with_retrieval(**retrieval: Any) -> dict[str, Any]:
        cfg = _pipeline()
        cfg["retrieval"] = {**cfg["retrieval"], **retrieval}
        return cfg

    return {
        "rrf": with_retrieval(),
        "weighted": with_retrieval(fusion={"method": "weighted", "weights": {"dense": 2.0, "sparse": 1.0}}),
        "expand": with_retrieval(expand={"neighbors": 1}),
        "rerank": with_retrieval(rerank={"provider": "ranker", "top_n": 10}, top_k=5),
        "dense": with_retrieval(sources=[{"id": "dense", "index": "vec", "top_k": 20}], fusion=None),
        "sparse": with_retrieval(sources=[{"id": "sparse", "index": "fts", "top_k": 20}], fusion=None),
    }


def _snapshot(client: TestClient, ctx: AppContext) -> dict[str, Any]:
    _add_providers(ctx)
    ctx.providers.create("ranker", "reranker", "http", {"base_url": RERANKER_URL, "model": "test"})
    cid = _corpus(ctx)
    for name, content in DOCS.items():
        _upload(client, cid, content, external_id=name)
    pipelines = {}
    for name, cfg in _variants().items():
        cfg["name"] = name
        pipelines[name] = create_pipeline(ctx, cid, cfg)
    drain(ctx)
    out: dict[str, Any] = {}
    for name, pid in pipelines.items():
        for query in QUERIES:
            response = client.post(f"/v1/corpora/{cid}/query", json={"pipeline_id": pid, "query": query, "top_k": 8})
            assert response.status_code == 200, response.text
            body = response.json()
            out[f"{name}/{query}"] = {
                "partial": body["partial"],
                "results": [
                    {
                        "text": r["text"],
                        "rank": r["rank"],
                        "score": round(r["score"], 9),
                        "sources": {k: [v["rank"], round(v["score"], 9)] for k, v in r["sources"].items()},
                        "before": r["context_before"],
                        "after": r["context_after"],
                    }
                    for r in body["results"]
                ],
            }
    return out


@pytest.fixture(autouse=True)
def stable_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal scores are ordered by chunk id, which derives from random document ids."""
    counter = itertools.count(1)

    def new_id() -> str:
        return f"{next(counter):08x}-0000-4000-8000-000000000000"

    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("ragcentre") and hasattr(module, "new_id"):
            monkeypatch.setattr(module, "new_id", new_id)


def test_pipelines_without_a_transform_return_what_they_always_did(client: TestClient, ctx: AppContext) -> None:
    snapshot = _snapshot(client, ctx)
    assert any(item["results"] for item in snapshot.values())
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")
    assert snapshot == json.loads(GOLDEN.read_text())
