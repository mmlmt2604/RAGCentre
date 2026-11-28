from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.providers.manager import destination_hash

CASES = [
    ("qdrant", {"url": "http://127.0.0.1:6333"}, "api_key", {"hnsw_ef": 77}),
    ("pgvector", {}, "dsn", {"ef_search": 77, "max_scan_tuples": 12345}),
    ("chroma", {}, "token", {"ef_search": 77}),
    ("lancedb", {}, "api_key", {"nprobes": 3}),
    ("redis", {"algorithm": "hnsw"}, "password", {"ef_runtime": 77}),
    ("weaviate", {"url": "http://127.0.0.1:8080"}, "api_key", {"hnsw_ef": 77}),
    ("elasticsearch", {"url": "http://127.0.0.1:9200"}, "api_key", {"num_candidates_factor": 17}),
    ("opensearch", {"url": "http://127.0.0.1:9200"}, "password", {"num_candidates_factor": 17}),
    ("milvus", {"uri": "http://127.0.0.1:19530", "index_type": "HNSW"}, "token", {"hnsw_ef": 77}),
    ("mongodb", {}, "uri", {"exact": True, "num_candidates_factor": 17}),
]


@pytest.mark.parametrize(("adapter", "config", "secret", "tuning"), CASES, ids=[case[0] for case in CASES])
def test_search_tuning_preserves_credentials_but_destination_changes_reset_them(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
    config: dict[str, Any],
    secret: str,
    tuning: dict[str, Any],
) -> None:
    monkeypatch.setenv("RAGCENTRE_PROVIDER_VECTOR_TEST_KEY", "fixture-credential")
    created = client.post(
        "/v1/providers",
        json={
            "name": "tuning-secret",
            "kind": "vector_store",
            "type": adapter,
            "config": config,
        },
    )
    assert created.status_code == 201, created.text
    provider = created.json()
    path = f"/v1/providers/{provider['id']}"
    bound = client.put(f"{path}/secrets/{secret}", json={"env": "RAGCENTRE_PROVIDER_VECTOR_TEST_KEY"})
    assert bound.status_code == 200, bound.text
    assert bound.json()["secrets"][secret]["set"] is True
    tuned = client.patch(path, json={"config": tuning})
    assert tuned.status_code == 200, tuned.text
    assert "X-RAGCentre-Secrets-Reset" not in tuned.headers
    assert tuned.json()["secrets"][secret]["set"] is True
    assert tuned.json()["fingerprint"] == provider["fingerprint"]
    assert client.get(path).json()["secrets"][secret]["set"] is True

    destination = {"schema": "another_schema"} if adapter == "pgvector" else {"prefix": "another_"}
    changed = client.patch(path, json={"config": destination | tuning})
    assert changed.status_code == 200, changed.text
    assert changed.headers["X-RAGCentre-Secrets-Reset"] == "true"
    assert changed.json()["secrets"][secret]["set"] is False


@pytest.mark.parametrize("binding", ["invalid", "future"])
def test_tuning_cannot_revive_an_invalid_secret_binding(
    ctx: AppContext, client: TestClient, monkeypatch: pytest.MonkeyPatch, binding: str
) -> None:
    monkeypatch.setenv("RAGCENTRE_PROVIDER_VECTOR_TEST_KEY", "fixture-credential")
    provider = ctx.providers.create("stale-binding", "vector_store", "pgvector", {})
    path = f"/v1/providers/{provider.id}"
    assert client.put(f"{path}/secrets/dsn", json={"env": "RAGCENTRE_PROVIDER_VECTOR_TEST_KEY"}).status_code == 200
    if binding == "future":
        binding = destination_hash(ctx.providers.spec(provider), provider.config | {"ef_search": 77})
    ctx.db.execute("UPDATE provider_secrets SET destination_hash = ? WHERE provider_id = ?", (binding, provider.id))
    assert client.get(path).json()["secrets"]["dsn"]["set"] is False
    changed = client.patch(path, json={"config": {"ef_search": 77}})
    assert changed.status_code == 200, changed.text
    assert changed.json()["secrets"]["dsn"]["set"] is False
