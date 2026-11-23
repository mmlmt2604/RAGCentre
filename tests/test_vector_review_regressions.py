"""Regression coverage for the final VectorDB compatibility review."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.stores.base import CollectionSpec, StoreError, check_collection_name
from ragcentre.stores.http import HttpStore, _backoff
from ragcentre.stores.pgvector import PgvectorStore

INFO = {
    "protocol": "ragcentre.vector_store/v1",
    "service_name": "review-shim",
    "service_version": "1.0",
    "capabilities": {
        "metrics": ["cosine"],
        "max_batch": 20,
        "max_dim": 128,
        "consistency": "eventual",
        "filter_exact": False,
        "max_collections": 1,
    },
}


def test_http_strips_reserved_document_field_and_retains_filters() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json=INFO)
        body = json.loads(request.content)
        seen.append(body)
        assert "document_id" not in body["fields"]
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://shim") as client:
        store = HttpStore.connect("http://shim", client=client)
        store.ensure_collection(CollectionSpec("rc_reserved_001", 8, "cosine", {"document_id": "str", "lang": "str"}))
    assert seen == [{"dim": 8, "metric": "cosine", "fields": {"lang": "str"}}]


def test_retry_after_is_a_minimum_including_http_dates() -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    assert _backoff(1, httpx.Response(503, headers={"Retry-After": "30"})) == 30
    assert _backoff(1, httpx.Response(503, headers={"Retry-After": "0"})) == 0.5
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)
    assert 58 < _backoff(0, httpx.Response(503, headers={"Retry-After": future})) <= 60
    assert _backoff(0, httpx.Response(503, headers={"Retry-After": "invalid"})) == 0.25


def test_http_provider_captures_and_refreshes_info(
    client: TestClient,
    ctx: AppContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {**INFO, "capabilities": dict(INFO["capabilities"])}
    auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            auth.append(request.headers.get("Authorization"))
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        ctx, "http_client_factory", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
    )
    made = client.post(
        "/v1/providers",
        json={
            "name": "remote-vectors",
            "kind": "vector_store",
            "type": "http",
            "config": {"url": "http://shim"},
        },
    )
    assert made.status_code == 201, made.text
    row = made.json()
    assert row["config"]["service_name"] == INFO["service_name"]
    assert row["config"]["service_version"] == "1.0"
    assert row["config"]["capabilities"]["filter_exact"] is False
    url = f"/v1/providers/{row['id']}"
    # Simulate a persisted provider created before /info metadata was captured.
    ctx.db.execute(
        "UPDATE providers SET config = ? WHERE id = ?",
        (json.dumps({"url": "http://shim", "timeout": 30}), row["id"]),
    )
    legacy_result = client.post(f"{url}/test").json()
    assert legacy_result["ok"] is True and legacy_result["version_drift"] is None
    refreshed = client.get(url).json()
    assert refreshed["config"]["service_version"] == "1.0"
    assert refreshed["fingerprint"] == row["fingerprint"]
    monkeypatch.setenv("RAGCENTRE_PROVIDER_REVIEW_VECTOR_TOKEN", "review-private-token")
    assert (
        client.put(f"{url}/secrets/api_key", json={"env": "RAGCENTRE_PROVIDER_REVIEW_VECTOR_TOKEN"}).status_code == 200
    )
    body["capabilities"] = {**body["capabilities"], "max_dim": 64}
    result = client.post(f"{url}/test")
    assert result.json()["ok"] is True, result.text
    assert client.get(url).json()["config"]["capabilities"]["max_dim"] == 64
    assert all(value is None for value in auth)
    refused = client.patch(url, json={"config": {"capabilities": {"filter_exact": True}}})
    assert refused.status_code == 409
    body["service_version"] = "2.0"
    assert client.post(f"{url}/test").json()["version_drift"] == {"pinned": "1.0", "current": "2.0"}


def test_http_provider_unreachable_at_creation(
    client: TestClient, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    monkeypatch.setattr(
        ctx, "http_client_factory", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
    )
    result = client.post(
        "/v1/providers",
        json={
            "name": "missing-vectors",
            "kind": "vector_store",
            "type": "http",
            "config": {"url": "http://shim"},
        },
    )
    assert result.status_code == 502, result.text
    assert result.json()["error"]["code"] == "provider_error"


def test_deep_test_redacts_every_finding(
    client: TestClient,
    ctx: AppContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ragcentre.stores.check import CheckReport

    secret = "review-super-secret-connection"
    made = client.post(
        "/v1/providers", json={"name": "redact-vectors", "kind": "vector_store", "type": "pgvector", "config": {}}
    )
    provider_id = made.json()["id"]
    monkeypatch.setenv("RAGCENTRE_PROVIDER_REVIEW_PG_DSN", secret)
    assert (
        client.put(
            f"/v1/providers/{provider_id}/secrets/dsn", json={"env": "RAGCENTRE_PROVIDER_REVIEW_PG_DSN"}
        ).status_code
        == 200
    )
    ctx.providers._resolve_secrets(provider_id)

    class Fake:
        def healthcheck(self) -> None:
            pass

    from contextlib import nullcontext

    monkeypatch.setattr(ctx.providers, "lease", lambda _id: nullcontext(Fake()))
    report = CheckReport("pgvector")
    report.add("error", "query", f"connection failed: {secret}")
    report.add("warn", "drop", f"cleanup failed: {secret}")
    monkeypatch.setattr("ragcentre.stores.check.check_store", lambda *a, **kw: report)
    result = client.post(f"/v1/providers/{provider_id}/test?deep=true")
    assert result.status_code == 200
    assert secret not in result.text
    assert result.json()["ok"] is False
    assert all(secret not in f["message"] for f in result.json()["check"]["findings"])


@pytest.mark.parametrize("name", ["rc_abc\n", "rc_abc\r", "rc_abc "])
def test_collection_name_rejects_trailing_whitespace(name: str) -> None:
    with pytest.raises(StoreError, match="invalid collection name"):
        check_collection_name(name)


def test_schema_name_rejects_trailing_newline() -> None:
    pytest.importorskip("psycopg")
    with pytest.raises(StoreError, match="invalid schema"):
        PgvectorStore("dbname=unused", schema="valid_schema\n")
