"""Release regressions for sessions after credential changes and credential boundaries."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ragcentre.accounts import SESSION_COOKIE
from ragcentre.accounts.passwords import validate_password, validate_username
from ragcentre.api.app import create_app
from ragcentre.cli import app as cli
from ragcentre.context import AppContext
from ragcentre.errors import ValidationFailed

PASSWORD = "release-test-original-password"
NEW_PASSWORD = "release-test-replacement-password"


@pytest.fixture(autouse=True)
def clear_limiter() -> Iterator[None]:
    from ragcentre.api.routers.auth import _limiter

    _limiter._buckets.clear()
    yield
    _limiter._buckets.clear()


def setup(client: TestClient) -> str:
    response = client.post("/v1/auth/setup", json={"username": "release-user", "password": PASSWORD})
    assert response.status_code == 201
    return str(response.json()["csrf"])


def login(client: TestClient, password: str = PASSWORD) -> str:
    response = client.post("/v1/auth/login", json={"username": "release-user", "password": password})
    assert response.status_code == 200
    return str(response.json()["csrf"])


@pytest.mark.parametrize("username", ["abc", "a" * 64, "  ReLease.User_1-2  "])
def test_username_valid_boundaries(username: str) -> None:
    assert validate_username(username) == username.strip().lower()


@pytest.mark.parametrize("username", ["", "ab", "a" * 65, ".abc", "_abc", "-abc", "a bc", "álîce", "abc\nxyz"])
def test_username_invalid_boundaries(username: str) -> None:
    with pytest.raises(ValidationFailed):
        validate_username(username)


@pytest.mark.parametrize("password", ["a" * 15, "a" * 128, "é" * 64, " " * 15, "🔒" * 32])
def test_password_valid_boundaries(password: str) -> None:
    validate_password(password)


@pytest.mark.parametrize("password", ["", "a" * 14, "a" * 129, "é" * 65, "🔒" * 33])
def test_password_invalid_boundaries(password: str) -> None:
    with pytest.raises(ValidationFailed):
        validate_password(password)


def test_new_sessions_work_after_repeated_password_changes(client: TestClient, ctx: AppContext) -> None:
    csrf = setup(client)
    current = PASSWORD
    for replacement in (NEW_PASSWORD, "release-test-third-password"):
        changed = client.post(
            "/v1/auth/password",
            json={"current_password": current, "new_password": replacement},
            headers={"X-RAGCentre-CSRF": csrf},
        )
        assert changed.status_code == 204
        assert client.get("/v1/auth/me").status_code == 401
        csrf = login(client, replacement)
        assert client.get("/v1/auth/me").status_code == 200, (
            "A fresh login must create a usable session after password change"
        )
        assert client.get("/v1/auth/csrf").json()["token"] == csrf
        assert (
            client.post("/v1/corpora", json={"name": replacement}, headers={"X-RAGCentre-CSRF": csrf}).status_code
            == 201
        )
        current = replacement
    assert ctx.db.scalar("SELECT auth_version FROM users") == 2


def test_cli_reset_allows_new_browser_session(
    client: TestClient, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup(client)
    monkeypatch.setenv("RAGCENTRE_DATA_DIR", str(ctx.settings.data_dir))
    reset = CliRunner().invoke(cli, ["account", "reset-password"], input=f"{NEW_PASSWORD}\n{NEW_PASSWORD}\n")
    assert reset.exit_code == 0
    assert NEW_PASSWORD not in reset.output
    assert client.get("/v1/auth/me").status_code == 401
    login(client, NEW_PASSWORD)
    assert client.get("/v1/auth/me").status_code == 200, "CLI recovery must produce usable browser sessions"


def test_password_change_revokes_both_sessions_but_keeps_keys(client: TestClient, ctx: AppContext) -> None:
    csrf = setup(client)
    key_response = client.post(
        "/v1/keys", json={"name": "surviving-key", "scope": "read"}, headers={"X-RAGCentre-CSRF": csrf}
    )
    assert key_response.status_code == 201
    key = key_response.json()["key"]
    with TestClient(create_app(ctx)) as other:
        login(other)
        response = client.post(
            "/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers={"X-RAGCentre-CSRF": csrf},
        )
        assert response.status_code == 204
        assert client.get("/v1/auth/me").status_code == 401
        assert other.get("/v1/auth/me").status_code == 401
        assert client.get("/v1/corpora", headers={"Authorization": f"Bearer {key}"}).status_code == 200
        login(other, NEW_PASSWORD)
        assert other.get("/v1/auth/me").status_code == 200


def test_wrong_current_password_preserves_sessions(client: TestClient, ctx: AppContext) -> None:
    csrf = setup(client)
    before = ctx.db.scalar("SELECT auth_version FROM users")
    response = client.post(
        "/v1/auth/password",
        json={"current_password": "incorrect-current-password", "new_password": NEW_PASSWORD},
        headers={"X-RAGCentre-CSRF": csrf},
    )
    assert response.status_code == 422
    assert ctx.db.scalar("SELECT auth_version FROM users") == before
    assert client.get("/v1/auth/me").status_code == 200
    login(client)
    assert client.get("/v1/auth/me").status_code == 200


def test_logout_only_revokes_current_session(client: TestClient, ctx: AppContext) -> None:
    csrf = setup(client)
    original_cookie = client.cookies.get(SESSION_COOKIE)
    with TestClient(create_app(ctx)) as other:
        login(other)
        assert client.post("/v1/auth/logout", headers={"X-RAGCentre-CSRF": csrf}).status_code == 204
        client.cookies.set(SESSION_COOKIE, str(original_cookie))
        assert client.get("/v1/auth/me").status_code == 401
        assert other.get("/v1/auth/me").status_code == 200


@pytest.mark.parametrize("field", ["is_platform_admin", "auth_version", "team_id", "scope"])
def test_setup_rejects_privilege_fields(client: TestClient, ctx: AppContext, field: str) -> None:
    response = client.post("/v1/auth/setup", json={"username": "release-user", "password": PASSWORD, field: "full"})
    assert response.status_code == 422
    assert not ctx.accounts.has_accounts()


@pytest.mark.parametrize("origin", ["http://foreign.example", "https://testserver", "http://testserver:9000", "null"])
def test_session_mutation_rejects_foreign_origin(client: TestClient, ctx: AppContext, origin: str) -> None:
    csrf = setup(client)
    before = ctx.db.scalar("SELECT COUNT(*) FROM corpora")
    response = client.post(
        "/v1/corpora",
        json={"name": "must-not-be-created"},
        headers={"X-RAGCentre-CSRF": csrf, "Origin": origin},
    )
    assert response.status_code == 403, "A valid CSRF token must not bypass the intended same-origin policy"
    assert ctx.db.scalar("SELECT COUNT(*) FROM corpora") == before


@pytest.mark.parametrize("csrf", [None, "incorrect-csrf"])
def test_invalid_csrf_does_not_create_resources(client: TestClient, ctx: AppContext, csrf: str | None) -> None:
    setup(client)
    before = ctx.db.scalar("SELECT COUNT(*) FROM corpora")
    headers = {"X-RAGCentre-CSRF": csrf} if csrf else {}
    response = client.post("/v1/corpora", json={"name": "must-not-be-created"}, headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_invalid"
    assert ctx.db.scalar("SELECT COUNT(*) FROM corpora") == before
