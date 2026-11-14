from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ragcentre.api.app import create_app
from ragcentre.context import AppContext, open_context
from ragcentre.settings import Settings
from ragcentre.testing.fakes import EMBEDDER_URL, PARSER_URL, RERANKER_URL, FakeServices


def create(client: TestClient, name: str, kind: str, type_: str, config: dict[str, object]) -> dict[str, object]:
    r = client.post("/v1/providers", json={"name": name, "kind": kind, "type": type_, "config": config})
    assert r.status_code == 201, r.text
    body: dict[str, object] = r.json()
    return body


def test_provider_types(client: TestClient) -> None:
    r = client.get("/v1/provider-types")
    assert r.status_code == 200
    items = {(t["kind"], t["type"]): t for t in r.json()["items"]}
    assert set(items) >= {
        ("parser", "builtin_text"),
        ("parser", "http"),
        ("chunker", "http"),
        ("embedder", "builtin_hash"),
        ("embedder", "openai_compatible"),
        ("reranker", "http"),
        ("vector_store", "sqlite_vec"),
    }
    sv = items[("vector_store", "sqlite_vec")]
    assert sv["capabilities"] == {"metrics": ["cosine", "l2"], "max_batch": 1000, "multi_vector": False,
                                  "native_hybrid": False}  # fmt: skip
    oa = items[("embedder", "openai_compatible")]
    assert oa["fingerprint_fields"] == [
        "model",
        "dimensions",
        "normalize",
        "query_prefix",
        "document_prefix",
        "revision",
    ]
    assert oa["secret_keys"] == ["api_key"]
    assert oa["config_schema"]["type"] == "object"
    assert "base_url" in oa["config_schema"]["properties"]
    assert set(oa["config_schema"]["required"]) == {"base_url", "model"}
    assert items[("parser", "builtin_text")]["capabilities"] is None
    assert set(items[("parser", "http")]) == {
        "kind", "type", "label", "description", "config_schema", "fingerprint_fields", "secret_keys", "capabilities",
    }  # fmt: skip


def test_builtin_text_provider_exists(client: TestClient) -> None:
    items = client.get("/v1/providers").json()["items"]
    builtin = [p for p in items if p["name"] == "builtin-text"]
    assert len(builtin) == 1
    p = builtin[0]
    assert p["builtin"] is True and p["kind"] == "parser" and p["type"] == "builtin_text"
    assert p["config"] == {"impl_version": 1}
    assert p["secrets"] == {}
    assert set(p) == {"id", "name", "kind", "type", "config", "fingerprint", "secrets", "builtin", "used_by_pipelines",
                      "created_at", "updated_at"}  # fmt: skip
    r = client.delete(f"/v1/providers/{p['id']}")
    assert r.status_code == 409


def test_builtin_created_once(settings: Settings, fakes: FakeServices) -> None:
    for _ in range(2):
        ctx = open_context(settings, http_client_factory=fakes.client_factory)
        assert len([p for p in ctx.providers.list() if p.name == "builtin-text"]) == 1
        ctx.close()


def test_operational_change_keeps_fingerprint_and_fingerprint_change_is_rejected(client: TestClient) -> None:
    p = create(client, "bge", "embedder", "openai_compatible", {"base_url": EMBEDDER_URL, "model": "bge-small"})
    fp = p["fingerprint"]
    r = client.patch(f"/v1/providers/{p['id']}", json={"config": {"base_url": "http://elsewhere:8080/v1",
                                                                  "batch_size": 8, "timeout_seconds": 5}})  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["fingerprint"] == fp
    assert r.json()["config"]["base_url"] == "http://elsewhere:8080/v1"
    assert r.json()["config"]["batch_size"] == 8
    # Re-sending a permanent field unchanged is fine (a form sends the whole config back).
    r = client.patch(f"/v1/providers/{p['id']}", json={"config": {**r.json()["config"]}})
    assert r.status_code == 200 and r.json()["fingerprint"] == fp
    for field, value in [("model", "bge-large"), ("dimensions", 128), ("normalize", True), ("query_prefix", "q: "),
                         ("revision", "2")]:  # fmt: skip
        r = client.patch(f"/v1/providers/{p['id']}", json={"config": {field: value}})
        assert r.status_code == 409, field
        assert r.json()["error"]["code"] == "immutable_field"
        assert r.json()["error"]["details"] == {"field": field}
    r = client.patch(f"/v1/providers/{p['id']}", json={"config": {"batch_size": 0}})
    assert r.status_code == 422
    r = client.patch(f"/v1/providers/{p['id']}", json={"config": {"nonsense": 1}})
    assert r.status_code == 422


def test_fingerprint_depends_only_on_fingerprint_fields(client: TestClient) -> None:
    a = create(client, "a", "embedder", "openai_compatible", {"base_url": "http://x/v1", "model": "m", "batch_size": 4})
    b = create(client, "b", "embedder", "openai_compatible", {"base_url": "http://y/v1", "model": "m", "batch_size": 9})
    c = create(
        client, "c", "embedder", "openai_compatible", {"base_url": "http://x/v1", "model": "m", "revision": "r2"}
    )
    assert a["fingerprint"] == b["fingerprint"] != c["fingerprint"]
    h1 = create(client, "h1", "embedder", "builtin_hash", {})
    h2 = create(client, "h2", "embedder", "builtin_hash", {"dimensions": 256})
    h3 = create(client, "h3", "embedder", "builtin_hash", {"dimensions": 64})
    assert h1["fingerprint"] == h2["fingerprint"] != h3["fingerprint"]
    assert h1["config"] == {"dimensions": 256, "impl_version": 1}
    s1 = create(client, "s1", "vector_store", "sqlite_vec", {})
    s2 = create(client, "s2", "vector_store", "sqlite_vec", {})
    assert s1["fingerprint"] != s2["fingerprint"]  # vector stores fingerprint by provider id


def test_create_validation(client: TestClient) -> None:
    def post(**body: object) -> int:
        return client.post(
            "/v1/providers", json={"kind": "embedder", "type": "builtin_hash", "config": {}, **body}
        ).status_code

    assert post(name="Bad Name") == 422
    assert post(name="ok", type="nope") == 422
    assert post(name="ok", kind="nope") == 422
    assert post(name="ok", config={"dimensions": "x"}) == 422
    assert post(name="ok", config={"impl_version": 2}) == 422  # fixed by the code, not the user
    assert post(name="ok") == 201
    assert post(name="ok") == 409


def test_http_parser_pins_service_version(client: TestClient, fakes: FakeServices) -> None:
    p = create(client, "docling", "parser", "http", {"base_url": PARSER_URL})
    assert p["config"]["service_name"] == "fake-parser"
    assert p["config"]["service_version"] == "1.0.0"
    assert p["config"]["on_version_drift"] == "fail"
    assert p["config"]["timeout_seconds"] == 600
    r = client.post(f"/v1/providers/{p['id']}/test").json()
    assert r["ok"] is True and r["version_drift"] is None
    assert r["service"] == {"name": "fake-parser", "version": "1.0.0", "mime_types": ["application/pdf", "text/*"]}
    fakes.parser_version = "1.1.0"
    r = client.post(f"/v1/providers/{p['id']}/test").json()
    assert r["ok"] is False
    assert r["version_drift"] == {"pinned": "1.0.0", "current": "1.1.0"}
    # A new provider created now pins the new version and gets a different fingerprint.
    q = create(client, "docling-2", "parser", "http", {"base_url": PARSER_URL})
    assert q["fingerprint"] != p["fingerprint"]
    r = client.patch(f"/v1/providers/{p['id']}", json={"config": {"service_version": "1.1.0"}})
    assert r.status_code == 409


def test_http_parser_unreachable_at_creation(client: TestClient) -> None:
    r = client.post("/v1/providers", json={"name": "nowhere", "kind": "parser", "type": "http",
                                           "config": {"base_url": "http://unknown.invalid"}})  # fmt: skip
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "provider_error"


def test_provider_test_results(client: TestClient, fakes: FakeServices) -> None:
    e = create(
        client, "emb", "embedder", "openai_compatible", {"base_url": EMBEDDER_URL, "model": "m", "dimensions": 32}
    )
    r = client.post(f"/v1/providers/{e['id']}/test").json()
    assert r["ok"] is True and r["dimensions"] == 32 and r["latency_ms"] >= 0
    assert set(r) == {"ok", "latency_ms", "message", "service", "dimensions", "version_drift"}
    h = create(client, "hash", "embedder", "builtin_hash", {"dimensions": 16})
    assert client.post(f"/v1/providers/{h['id']}/test").json()["dimensions"] == 16
    rr = create(client, "rr", "reranker", "http", {"base_url": RERANKER_URL, "model": "m"})
    assert client.post(f"/v1/providers/{rr['id']}/test").json()["ok"] is True
    s = create(client, "vec", "vector_store", "sqlite_vec", {})
    assert client.post(f"/v1/providers/{s['id']}/test").json()["ok"] is True
    builtin = next(p for p in client.get("/v1/providers").json()["items"] if p["name"] == "builtin-text")
    r = client.post(f"/v1/providers/{builtin['id']}/test").json()
    assert r["ok"] is True and r["service"]["mime_types"] == ["text/plain", "text/markdown"]
    fakes.script("embedder", "/embeddings", [500])
    r = client.post(f"/v1/providers/{e['id']}/test").json()
    assert r["ok"] is False and "500" in r["message"]


def test_delete_provider_and_in_use(client: TestClient, ctx: AppContext) -> None:
    p = create(client, "tmp", "embedder", "builtin_hash", {})
    q = create(client, "used", "embedder", "builtin_hash", {})
    ctx.db.execute("INSERT INTO corpora (id, name, metadata_schema, created_at) VALUES ('c', 'c', '{}', 'now')")
    ctx.db.execute(
        "INSERT INTO pipelines (id, corpus_id, name, config, resolved, fingerprint, created_at) "
        "VALUES ('pl', 'c', 'pl', '{}', ?, 'fp', 'now')",
        (json.dumps({"provider_ids": [q["id"]]}),),
    )
    assert client.get(f"/v1/providers/{q['id']}").json()["used_by_pipelines"] == 1
    r = client.delete(f"/v1/providers/{q['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "provider_in_use"
    ctx.db.execute("UPDATE pipelines SET archived = 1")
    assert client.get(f"/v1/providers/{q['id']}").json()["used_by_pipelines"] == 0
    assert client.delete(f"/v1/providers/{q['id']}").status_code == 204
    assert client.delete(f"/v1/providers/{p['id']}").status_code == 204
    assert client.get(f"/v1/providers/{p['id']}").status_code == 404


def test_pagination(client: TestClient) -> None:
    for i in range(5):
        create(client, f"h{i}", "embedder", "builtin_hash", {})
    first = client.get("/v1/providers", params={"limit": 4}).json()
    assert len(first["items"]) == 4 and first["next_cursor"]
    second = client.get("/v1/providers", params={"limit": 4, "cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    assert client.get("/v1/providers", params={"limit": 501}).status_code == 422


# ---- secrets ------------------------------------------------------------------------------------


def test_env_secret(client: TestClient, monkeypatch: pytest.MonkeyPatch, fakes: FakeServices) -> None:
    monkeypatch.setenv("FAKE_EMBED_KEY", "env-secret-value-123")
    p = create(client, "emb", "embedder", "openai_compatible", {"base_url": EMBEDDER_URL, "model": "m"})
    assert p["secrets"] == {"api_key": {"source": "env", "set": False, "env_var": None}}
    r = client.put(f"/v1/providers/{p['id']}/secrets/api_key", json={"env": "FAKE_EMBED_KEY"})
    assert r.status_code == 200
    assert r.json()["secrets"] == {"api_key": {"source": "env", "set": True, "env_var": "FAKE_EMBED_KEY"}}
    assert "env-secret-value-123" not in r.text
    assert client.post(f"/v1/providers/{p['id']}/test").json()["ok"] is True
    assert fakes.requests[-1].headers["authorization"] == "Bearer env-secret-value-123"
    assert client.put(f"/v1/providers/{p['id']}/secrets/nope", json={"env": "X"}).status_code == 422
    assert client.put(f"/v1/providers/{p['id']}/secrets/api_key", json={}).status_code == 422
    r = client.delete(f"/v1/providers/{p['id']}/secrets/api_key")
    assert r.json()["secrets"]["api_key"]["set"] is False


def test_stored_secret_refused_without_key(client: TestClient) -> None:
    p = create(client, "emb", "embedder", "openai_compatible", {"base_url": EMBEDDER_URL, "model": "m"})
    r = client.put(f"/v1/providers/{p['id']}/secrets/api_key", json={"value": "sk-abc"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "secret_storage_disabled"
    assert client.get("/v1/health").json()["secret_storage_enabled"] is False


def test_no_secret_in_any_response_or_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    secret = "sk-live-very-secret-0042"
    fakes = FakeServices()
    ctx = open_context(Settings(data_dir=tmp_path, secret_key=Fernet.generate_key().decode()),
                       http_client_factory=fakes.client_factory)  # fmt: skip
    caplog.set_level(logging.DEBUG, logger="ragcentre")
    bodies: list[str] = []
    try:
        with TestClient(create_app(ctx)) as client:
            assert client.get("/v1/health").json()["secret_storage_enabled"] is True
            p = create(client, "emb", "embedder", "openai_compatible", {"base_url": EMBEDDER_URL, "model": "m"})
            r = client.put(f"/v1/providers/{p['id']}/secrets/api_key", json={"value": secret})
            assert r.status_code == 200
            assert r.json()["secrets"] == {"api_key": {"source": "stored", "set": True, "env_var": None}}
            bodies.append(r.text)
            stored = ctx.db.scalar("SELECT value FROM provider_secrets")
            assert secret.encode() not in bytes(stored)
            # The provider receives the decrypted value...
            ok = client.post(f"/v1/providers/{p['id']}/test")
            assert ok.json()["ok"] is True
            assert fakes.requests[-1].headers["authorization"] == f"Bearer {secret}"
            # ...and an error that echoes it back is redacted before it reaches the API or a log.
            fakes.echo_auth_in_errors = True
            bad = client.post(f"/v1/providers/{p['id']}/test")
            assert bad.json()["ok"] is False
            assert "[REDACTED]" in bad.json()["message"]
            logging.getLogger("ragcentre.test").error("provider said %s", bad.json()["message"])
            logging.getLogger("ragcentre.test").error("raw value %s", secret)
            for path in ("/v1/providers", f"/v1/providers/{p['id']}", "/v1/provider-types"):
                bodies.append(client.get(path).text)
            bodies.append(bad.text)
    finally:
        ctx.close()
    for body in bodies:
        assert secret not in body
    from ragcentre.logs import JsonFormatter

    formatted = [JsonFormatter().format(rec) for rec in caplog.records]
    assert formatted and all(secret not in line for line in formatted)
    assert all(secret not in rec.getMessage() for rec in caplog.records)
    assert secret not in caplog.text
