"""Deterministic credential races at the expensive verification/hash boundaries."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from ragcentre.accounts import service
from ragcentre.api.app import create_app
from ragcentre.context import AppContext, open_context
from ragcentre.errors import InvalidCredentials

PASSWORD = "release-original-password"
RECOVERED = "release-operator-recovery-password"


def test_reset_during_login_verification_cannot_issue_stale_session(
    ctx: AppContext, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = ctx.accounts.create_account("race-user", "", PASSWORD)
    verify = service.verify_password

    def reset_while_verifying(encoded: str, supplied: str) -> bool:
        ctx.accounts.reset_password(user["id"], RECOVERED)
        return verify(encoded, supplied)

    monkeypatch.setattr(service, "verify_password", reset_while_verifying)
    response = client.post("/v1/auth/login", json={"username": "race-user", "password": PASSWORD})
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert ctx.db.scalar("SELECT COUNT(*) FROM user_sessions") == 0
    monkeypatch.setattr(service, "verify_password", verify)
    response = client.post("/v1/auth/login", json={"username": "race-user", "password": RECOVERED})
    assert response.status_code == 200
    assert client.get("/v1/auth/me").status_code == 200


@pytest.mark.parametrize("change", ["reset", "disable", "delete"])
def test_session_issuance_rechecks_verified_account(ctx: AppContext, change: str) -> None:
    user = ctx.accounts.create_account("race-user", "", PASSWORD)
    verified = ctx.accounts.verify_login("race-user", PASSWORD)
    assert verified is not None
    old_token, _ = ctx.accounts.create_session(user["id"], expected_auth_version=verified["auth_version"])
    if change == "reset":
        ctx.accounts.reset_password(user["id"], RECOVERED)
    elif change == "disable":
        ctx.db.execute("UPDATE users SET status = 'disabled' WHERE id = ?", (user["id"],))
    else:
        ctx.db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    with pytest.raises(InvalidCredentials):
        ctx.accounts.create_session(user["id"], expected_auth_version=verified["auth_version"])
    assert ctx.accounts.session_for(old_token) is None


def test_recovery_persists_for_independently_reopened_context(ctx: AppContext) -> None:
    user = ctx.accounts.create_account("race-user", "", PASSWORD)
    old_token, _ = ctx.accounts.create_session(user["id"], expected_auth_version=user["auth_version"])
    key = ctx.accounts.create_key("surviving-recovery", "read")["key"]
    ctx.accounts.reset_password(user["id"], RECOVERED)
    reopened = open_context(ctx.settings)
    try:
        assert reopened.accounts.session_for(old_token) is None
        assert reopened.accounts.verify_login("race-user", PASSWORD) is None
        assert reopened.accounts.key_for(key) is not None
        with TestClient(create_app(reopened)) as client:
            response = client.post("/v1/auth/login", json={"username": "race-user", "password": RECOVERED})
            assert response.status_code == 200
            assert client.get("/v1/auth/me").status_code == 200
            assert (
                client.post(
                    "/v1/corpora",
                    json={"name": "after-reopen"},
                    headers={"X-RAGCentre-CSRF": response.json()["csrf"]},
                ).status_code
                == 201
            )
    finally:
        reopened.close()


@pytest.mark.parametrize("boundary", ["verify", "hash"])
def test_stale_browser_password_change_cannot_overwrite_operator_reset(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    user = ctx.accounts.create_account("race-user", "", PASSWORD)
    replacement = "release-stale-browser-password"
    original: Callable[..., object] = getattr(service, f"{boundary}_password")
    triggered = False

    def recover(*args: str) -> object:
        nonlocal triggered
        if not triggered:
            triggered = True
            ctx.accounts.reset_password(user["id"], RECOVERED)
        return original(*args)

    monkeypatch.setattr(service, f"{boundary}_password", recover)
    with pytest.raises(InvalidCredentials):
        ctx.accounts.change_password(user["id"], PASSWORD, replacement)
    assert triggered
    monkeypatch.setattr(service, f"{boundary}_password", original)
    assert ctx.accounts.verify_login("race-user", replacement) is None
    assert ctx.accounts.verify_login("race-user", RECOVERED) is not None
    assert ctx.db.scalar("SELECT auth_version FROM users") == 1
