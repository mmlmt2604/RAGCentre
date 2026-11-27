"""Positive read-key controls with real parsed/indexed data and session invalidation."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.api.app import create_app
from ragcentre.context import AppContext
from ragcentre.pipeline.planner import create_pipeline
from ragcentre.settings import Settings
from test_m6_ingestion import MD, _add_providers, _corpus, _pipeline, _upload
from test_m7_retrieval import drain


@pytest.fixture
def populated(ctx: AppContext, client: TestClient) -> dict[str, Any]:
    _add_providers(ctx)
    cid = _corpus(ctx)
    doc = _upload(client, cid, MD)
    pid = create_pipeline(ctx, cid, _pipeline())
    drain(ctx)
    ctx.db.execute("UPDATE corpora SET active_pipeline_id = ? WHERE id = ?", (pid, cid))
    user = ctx.accounts.create_account("read-control", "", "read-control-password")
    token, csrf = ctx.accounts.create_session(user["id"], expected_auth_version=user["auth_version"])
    key = ctx.accounts.create_key("populated-read", "read")
    return {"corpus": cid, "document": doc["id"], "pipeline": pid, "token": token, "csrf": csrf, "key": key["key"]}


@pytest.mark.parametrize(
    "route",
    [
        "/v1/corpora",
        "/v1/corpora/{corpus}",
        "/v1/corpora/{corpus}/documents",
        "/v1/corpora/{corpus}/pipelines",
        "/v1/documents/{document}",
        "/v1/documents/{document}/chunks?pipeline_id={pipeline}",
        "/v1/documents/{document}/content",
        "/v1/documents/{document}/parsed?pipeline_id={pipeline}",
        "/v1/pipelines/{pipeline}/status",
    ],
)
def test_read_key_reads_populated_resources(client: TestClient, populated: dict[str, Any], route: str) -> None:
    response = client.get(route.format(**populated), headers={"Authorization": f"Bearer {populated['key']}"})
    assert response.status_code == 200, route
    assert response.content
    assert populated["key"] not in response.text


def test_read_key_can_query_real_index_without_csrf(client: TestClient, populated: dict[str, Any]) -> None:
    response = client.post(
        f"/v1/corpora/{populated['corpus']}/query",
        json={"query": "warranty"},
        headers={"Authorization": f"Bearer {populated['key']}"},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results
    assert all(item["document_id"] == populated["document"] for item in results)


@pytest.mark.parametrize("mutation", ["expires_at", "auth_version", "disabled", "missing"])
def test_stale_sessions_cannot_read_private_resources(ctx: AppContext, client: TestClient, mutation: str) -> None:
    setup = client.post("/v1/auth/setup", json={"username": "stale-session", "password": "stale-session-password"})
    assert setup.status_code == 201
    assert client.get("/v1/auth/me").status_code == 200
    if mutation == "expires_at":
        ctx.db.execute("UPDATE user_sessions SET expires_at = '2000-01-01T00:00:00Z'")
    elif mutation == "auth_version":
        ctx.db.execute("UPDATE users SET auth_version = auth_version + 1")
    elif mutation == "disabled":
        ctx.db.execute("UPDATE users SET status = 'disabled'")
    else:
        ctx.db.execute("DELETE FROM user_sessions")
    assert client.get("/v1/auth/me").status_code == 401
    assert client.get("/v1/corpora").status_code == 401


@pytest.mark.parametrize("scheme,secure", [("http", False), ("https", True)])
def test_session_cookie_attributes(ctx: AppContext, scheme: str, secure: bool) -> None:
    with TestClient(create_app(ctx), base_url=f"{scheme}://testserver") as browser:
        response = browser.post(
            "/v1/auth/setup", json={"username": "cookie-control", "password": "cookie-control-password"}
        )
        assert response.status_code == 201
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
        assert "max-age=2592000" in cookie and "domain=" not in cookie
        assert ("; secure" in cookie) is secure
        assert browser.get("/v1/auth/me").status_code == 200


@pytest.mark.parametrize("value", ["1", "true", "yes", " TRUE ", "YeS"])
def test_remote_setup_true_values(value: str) -> None:
    assert Settings.from_env({"RAGCENTRE_ALLOW_REMOTE_SETUP": value}).allow_remote_setup


@pytest.mark.parametrize("value", ["", "0", "false", "no", "anything", "2"])
def test_remote_setup_false_values(value: str) -> None:
    assert not Settings.from_env({"RAGCENTRE_ALLOW_REMOTE_SETUP": value}).allow_remote_setup
