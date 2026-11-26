"""Regression checks for provider isolation and document lifecycle guarantees."""

from __future__ import annotations

import functools
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.db.connection import Database
from ragcentre.db.migrate import available_migrations, migrate
from ragcentre.errors import ExecutionError
from ragcentre.pipeline.planner import create_pipeline, rebuild_broken_indexes
from ragcentre.providers.egress import checked_http_client
from ragcentre.queue.tasks import run_task
from ragcentre.testing.fakes import EMBEDDER_URL
from test_m6_ingestion import _add_providers, _corpus, _pipeline, _upload
from test_m7_retrieval import drain


def test_provider_secret_follows_destination_and_active_lease(
    ctx: AppContext, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ctx.providers.create("scoped", "embedder", "openai_compatible", {"base_url": EMBEDDER_URL, "model": "m"})
    path = f"/v1/providers/{provider.id}"
    name = "RAGCENTRE_PROVIDER_TEST"
    monkeypatch.setenv(name, "test-provider-key")
    assert client.put(f"{path}/secrets/api_key", json={"env": name}).status_code == 200
    assert client.patch(path, json={"config": {"timeout_seconds": 5}}).json()["secrets"]["api_key"]["set"]

    with ctx.providers.lease(provider.id) as current:
        assert current.client.headers["authorization"] == "Bearer test-provider-key"
        response = client.patch(path, json={"config": {"base_url": "http://localhost:54321/v1"}})
        assert response.headers["X-RAGCentre-Secrets-Reset"] == "true"
        assert not response.json()["secrets"]["api_key"]["set"]
        assert not current.client.is_closed
        with ctx.providers.lease(provider.id) as replacement:
            assert "authorization" not in replacement.client.headers
    assert current.client.is_closed


def test_provider_validates_environment_references_and_store_paths(ctx: AppContext, client: TestClient) -> None:
    provider = ctx.providers.create("scoped", "embedder", "openai_compatible", {"base_url": EMBEDDER_URL, "model": "m"})
    path = f"/v1/providers/{provider.id}/secrets/api_key"
    assert client.put(path, json={"env": "RAGCENTRE_API_TOKEN"}).status_code == 422

    stores = ctx.settings.data_dir / "stores"
    stores.mkdir()
    (stores / "outside").symlink_to(ctx.settings.data_dir, target_is_directory=True)
    for value in ("../vectors.db", str(ctx.settings.db_path), "outside/vectors.db"):
        response = client.post(
            "/v1/providers",
            json={"name": "bad-path", "kind": "vector_store", "type": "sqlite_vec", "config": {"path": value}},
        )
        assert response.status_code == 422


def test_upgraded_provider_requires_secret_rebinding(ctx: AppContext) -> None:
    provider = ctx.providers.create("legacy", "embedder", "openai_compatible", {"base_url": EMBEDDER_URL, "model": "m"})
    ctx.db.execute(
        "INSERT INTO provider_secrets (provider_id, key, source, value) VALUES (?, 'api_key', 'env', ?)",
        (provider.id, b"RAGCENTRE_PROVIDER_LEGACY_KEY"),
    )
    with pytest.raises(ExecutionError, match="Set the provider's secret again"):
        ctx.providers.instance(provider.id)


def test_http_transport_applies_allowlist_before_connecting() -> None:
    with checked_http_client(("192.0.2.0/24",)) as client, pytest.raises(httpx.ConnectError, match="blocked"):
        client.get("http://127.0.0.1:1")


def test_legacy_index_collisions_are_quarantined(tmp_path: Path) -> None:
    db = Database(tmp_path / "legacy.db")
    try:
        with db.connection() as conn:
            for migration in available_migrations()[:2]:
                conn.executescript(migration.sql)
            conn.execute("PRAGMA user_version = 2")
            conn.executemany(
                "INSERT INTO indexes (id, corpus_id, node_fp, kind, physical_name, created_at) "
                "VALUES (?, ?, 'fingerprint', 'fts', ?, 'now')",
                [("1", "a", "legacy_shared"), ("2", "b", "legacy_shared"), ("3", "c", "legacy_distinct")],
            )
        assert migrate(db) == [3, 4, 5, 6, 7, 8, 9]
        assert [r["broken"] for r in db.all("SELECT broken FROM indexes ORDER BY id")] == [1, 1, 0]
    finally:
        db.close()


def test_corpus_isolation_and_index_rebuild(ctx: AppContext, client: TestClient) -> None:
    _add_providers(ctx)
    a, b = _corpus(ctx, "a"), _corpus(ctx, "b")
    pipeline_a = create_pipeline(ctx, a, _pipeline())
    pipeline_b = create_pipeline(ctx, b, _pipeline())
    document_a = _upload(client, a, b"Test document.")
    drain(ctx)
    for endpoint in ("parsed", "chunks"):
        response = client.get(f"/v1/documents/{document_a['id']}/{endpoint}", params={"pipeline_id": pipeline_b})
        assert response.status_code == 404
    index = ctx.db.one("SELECT * FROM indexes WHERE corpus_id = ? AND kind = 'fts'", (a,))
    assert index is not None
    ctx.db.execute("UPDATE indexes SET broken=1 WHERE id=?", (index["id"],))
    failed = client.post(f"/v1/corpora/{a}/query", json={"pipeline_id": pipeline_a, "query": "test"})
    assert failed.json()["error"]["code"] == "index_mismatch"
    assert rebuild_broken_indexes(ctx) == 1
    drain(ctx)
    assert client.post(f"/v1/corpora/{a}/query", json={"pipeline_id": pipeline_a, "query": "test"}).is_success


def test_reupload_and_delete_keep_only_live_references(ctx: AppContext, client: TestClient) -> None:
    _add_providers(ctx)
    corpus = _corpus(ctx)
    ctx.db.execute(
        "UPDATE corpora SET metadata_schema = ? WHERE id = ?",
        (json.dumps({"fields": {"lang": {"type": "str", "default": "en"}}}), corpus),
    )
    create_pipeline(ctx, corpus, _pipeline())
    canary = b"unique retention marker d41f8a4a4737"
    doc = _upload(client, corpus, canary, external_id="doc", metadata={"lang": "de"})
    duplicate = _upload(client, corpus, canary, external_id="copy")
    drain(ctx)
    sha = doc["content_sha256"]
    cached = ctx.db.scalar("SELECT blob_sha256 FROM parse_cache WHERE content_sha256 = ?", (sha,))
    assert cached and ctx.blobs.exists(cached)

    post = functools.partial(client.post, f"/v1/corpora/{corpus}/documents")
    files = {"file": ("same.md", canary, "text/markdown")}
    assert post(files=files, data={"external_id": "doc"}).json()["metadata"] == {"lang": "de"}
    assert post(files=files, data={"external_id": "doc", "metadata": "{}"}).json()["metadata"] == {}

    def remove(doc_id: str) -> None:
        response = client.delete(f"/v1/documents/{doc_id}")
        assert response.status_code == 202
        assert client.get(f"/v1/documents/{doc_id}/content").status_code == 404
        assert client.patch(f"/v1/documents/{doc_id}", json={"attributes": {}}).status_code == 409
        run_task(ctx, {"kind": "delete_document", "payload": json.dumps({"document_id": doc_id})})

    content = client.get(f"/v1/documents/{doc['id']}/content")
    assert content.headers["content-disposition"].startswith("attachment;")
    assert content.headers["x-content-type-options"] == "nosniff"
    assert content.headers["content-security-policy"] == "sandbox"
    remove(doc["id"])
    assert ctx.blobs.exists(sha) and ctx.blobs.exists(cached)
    remove(duplicate["id"])
    assert not ctx.blobs.exists(sha) and not ctx.blobs.exists(cached)
    assert canary not in ctx.settings.db_path.read_bytes()
    wal = ctx.settings.db_path.with_name(ctx.settings.db_path.name + "-wal")
    assert not wal.exists() or canary not in wal.read_bytes()
