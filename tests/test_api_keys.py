from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ragcentre.api.app import create_app
from ragcentre.context import open_context
from ragcentre.settings import Settings

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _clear_login_limiter() -> None:
    from ragcentre.api.routers.auth import _limiter

    _limiter._buckets.clear()
    yield
    _limiter._buckets.clear()


def _setup(client: TestClient) -> dict:
    resp = client.post("/v1/auth/setup", json={"username": "alice", "display_name": "Alice", "password": PASSWORD})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_key(client: TestClient, csrf: str, name: str = "mcp", scope: str = "read") -> dict:
    resp = client.post("/v1/keys", json={"name": name, "scope": scope}, headers={"X-RAGCentre-CSRF": csrf})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_list_and_revoke_keys(client: TestClient) -> None:
    body = _setup(client)
    csrf = body["csrf"]

    created = _create_key(client, csrf, "claude-mcp", "read")
    assert created["name"] == "claude-mcp"
    assert created["scope"] == "read"
    assert created["key"]

    listed = client.get("/v1/keys").json()["items"]
    assert [k["name"] for k in listed] == ["claude-mcp"]
    # The secret is never returned by the list.
    assert all("key" not in k for k in listed)

    assert client.delete(f"/v1/keys/{created['id']}", headers={"X-RAGCentre-CSRF": csrf}).status_code == 204
    assert client.get("/v1/keys").json()["items"] == []


def test_keys_endpoints_are_session_only(client: TestClient) -> None:
    # No account and no token: the loopback dev guard is open, but the endpoint still demands a session.
    assert client.get("/v1/keys").status_code == 401

    _setup(client)
    csrf = client.get("/v1/auth/csrf").json()["token"]
    full = _create_key(client, csrf, "cli", "full")
    headers = {"Authorization": f"Bearer {full['key']}"}
    # A full key is also rejected: key management is a browser-session action.
    assert client.get("/v1/keys", headers=headers).status_code == 401


def test_read_key_is_confined_to_read_routes(client: TestClient) -> None:
    body = _setup(client)
    read = _create_key(client, body["csrf"], "mcp", "read")
    headers = {"Authorization": f"Bearer {read['key']}"}

    assert client.get("/v1/auth/whoami", headers=headers).json() == {"scope": "read", "kind": "key"}
    assert client.get("/v1/corpora", headers=headers).status_code == 200
    # Read keys cannot read providers, admin, node runs, eval, or anything else.
    assert client.get("/v1/providers", headers=headers).status_code == 403
    assert client.get("/v1/node-runs", headers=headers).status_code == 403
    # Read keys cannot mutate.
    assert client.post("/v1/corpora", json={"name": "c"}, headers=headers).status_code == 403


def test_full_key_can_manage_resources(client: TestClient) -> None:
    body = _setup(client)
    full = _create_key(client, body["csrf"], "cli", "full")
    headers = {"Authorization": f"Bearer {full['key']}"}

    assert client.get("/v1/auth/whoami", headers=headers).json() == {"scope": "full", "kind": "key"}
    assert client.get("/v1/providers", headers=headers).status_code == 200
    assert client.post("/v1/corpora", json={"name": "c"}, headers=headers).status_code == 201


def test_env_token_reports_admin_scope(tmp_path: Path) -> None:
    ctx = open_context(Settings(data_dir=tmp_path, api_token="test-token"))
    try:
        with TestClient(create_app(ctx)) as client:
            headers = {"Authorization": "Bearer test-token"}
            assert client.get("/v1/auth/whoami", headers=headers).json() == {"scope": "full", "kind": "admin_token"}
            # The env token is full, but account/key endpoints stay session-only.
            assert client.get("/v1/keys", headers=headers).status_code == 401
    finally:
        ctx.close()
