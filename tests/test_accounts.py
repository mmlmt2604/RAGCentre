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


def _setup(client: TestClient, username: str = "alice", password: str = PASSWORD) -> dict:
    resp = client.post("/v1/auth/setup", json={"username": username, "display_name": "Alice", "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_config_before_account(client: TestClient) -> None:
    data = client.get("/v1/auth/config").json()
    assert data == {"auth_required": False, "setup_required": True, "login_available": False, "setup_allowed": True}


def test_anonymous_access_without_account_or_token(client: TestClient) -> None:
    # No account and no token: the legacy open-loopback behaviour is preserved.
    assert client.get("/v1/corpora").status_code == 200


def test_setup_creates_account_and_session(client: TestClient) -> None:
    body = _setup(client)
    assert body["user"]["username"] == "alice"
    assert body["user"]["is_platform_admin"] is True
    assert body["csrf"]

    me = client.get("/v1/auth/me").json()
    assert me["username"] == "alice"
    assert me["is_platform_admin"] is True

    # A second setup is refused now that an account exists.
    assert client.post("/v1/auth/setup", json={"username": "bob", "password": PASSWORD}).status_code == 403


def test_setup_rejects_short_password(client: TestClient) -> None:
    resp = client.post("/v1/auth/setup", json={"username": "alice", "password": "short"})
    assert resp.status_code == 422


def test_setup_refused_on_non_loopback(tmp_path: Path) -> None:
    ctx = open_context(Settings(data_dir=tmp_path, host="0.0.0.0"))
    try:
        with TestClient(create_app(ctx)) as client:
            resp = client.post("/v1/auth/setup", json={"username": "alice", "password": PASSWORD})
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "setup_unavailable"
    finally:
        ctx.close()


def test_setup_allowed_on_non_loopback_with_opt_in(tmp_path: Path) -> None:
    ctx = open_context(Settings(data_dir=tmp_path, host="0.0.0.0", allow_remote_setup=True))
    try:
        with TestClient(create_app(ctx)) as client:
            assert client.post("/v1/auth/setup", json={"username": "alice", "password": PASSWORD}).status_code == 201
    finally:
        ctx.close()


def test_login_and_logout(client: TestClient) -> None:
    body = _setup(client)
    assert client.post("/v1/auth/logout", headers={"X-RAGCentre-CSRF": body["csrf"]}).status_code == 204
    assert client.get("/v1/auth/me").status_code == 401

    # Wrong password is a generic 401 that names neither the account nor the field.
    wrong = client.post("/v1/auth/login", json={"username": "alice", "password": "wrong-password-here"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "invalid_credentials"

    ok = client.post("/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert ok.status_code == 200
    assert ok.json()["user"]["username"] == "alice"

    assert client.post("/v1/auth/logout", headers={"X-RAGCentre-CSRF": ok.json()["csrf"]}).status_code == 204
    assert client.get("/v1/auth/me").status_code == 401


def test_login_requires_csrf_for_state_changes(client: TestClient) -> None:
    _setup(client)
    assert client.get("/v1/auth/csrf").status_code == 200
    # A session state change without the CSRF header is refused.
    assert client.post("/v1/auth/logout").status_code == 403


def test_password_change_revokes_sessions(client: TestClient) -> None:
    body = _setup(client)
    resp = client.post(
        "/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "a-brand-new-password-42"},
        headers={"X-RAGCentre-CSRF": body["csrf"]},
    )
    assert resp.status_code == 204
    # The session used to change the password is gone.
    assert client.get("/v1/auth/me").status_code == 401
    # The old password no longer works; the new one does.
    assert client.post("/v1/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 401
    assert (
        client.post("/v1/auth/login", json={"username": "alice", "password": "a-brand-new-password-42"}).status_code
        == 200
    )


def test_login_rate_limited(client: TestClient) -> None:
    _setup(client)
    for _ in range(5):
        resp = client.post("/v1/auth/login", json={"username": "alice", "password": "wrong-password-here"})
        assert resp.status_code == 401
    resp = client.post("/v1/auth/login", json={"username": "alice", "password": "wrong-password-here"})
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "too_many_requests"


def test_whoami_anonymous(client: TestClient) -> None:
    assert client.get("/v1/auth/whoami").json() == {"scope": "none", "kind": "none"}
