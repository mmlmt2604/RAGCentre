"""Browser recovery remains bounded, purpose-bound and independent of account activation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.accounts import recovery
from ragcentre.accounts.users import patch_user, reset_password
from ragcentre.errors import RagError
from ragcentre.timeutil import after
from test_local_accounts import ORIGIN, PASSWORD, _account
from test_local_accounts import local as local

NEW_PASSWORD = "a different long recovery password"


def _issue(local: Any, user_id: str, password: str = PASSWORD):
    _, _, admin, headers, _ = local
    return admin.post(f"/v1/users/{user_id}/recovery-token", json={"current_password": password}, headers=headers)


def _inspect(client: TestClient, token: str):
    return client.post("/v1/auth/recovery/inspect", json={"token": token}, headers={"Origin": ORIGIN})


def _complete(client: TestClient, token: str):
    return client.post(
        "/v1/auth/recovery/complete", json={"token": token, "password": NEW_PASSWORD}, headers={"Origin": ORIGIN}
    )


def test_recovery_requires_admin_password_does_not_revoke_until_complete_and_never_logs_in(local: Any) -> None:
    ctx, app, _, _, _ = local
    target, _, data = _account(local, "recoverable")
    try:
        uid = data["user"]["id"]
        assert _issue(local, uid, "wrong password").status_code == 401
        issued = _issue(local, uid)
        assert issued.status_code == 200, issued.text
        token = issued.json()["token"]
        assert target.get("/v1/auth/me").status_code == 200
        with TestClient(app, base_url=ORIGIN) as fresh:
            result = _inspect(fresh, token)
            assert result.status_code == 200 and result.json()["username"] == "recoverable"
            assert _inspect(fresh, token).status_code == 200
            response = _complete(fresh, token)
            assert response.status_code == 204 and "set-cookie" not in response.headers
            assert fresh.get("/v1/auth/me").status_code == 401
            assert _complete(fresh, token).status_code == 422
            login = fresh.post(
                "/v1/auth/login", json={"username": "recoverable", "password": NEW_PASSWORD}, headers={"Origin": ORIGIN}
            )
            assert login.status_code == 200
        assert target.get("/v1/auth/me").status_code == 401
        assert ctx.db.scalar("SELECT status FROM users WHERE id=?", (uid,)) == "active"
        assert ctx.db.scalar("SELECT count(*) FROM team_memberships WHERE user_id=?", (uid,)) == 1
        assert token not in str([dict(r) for r in ctx.db.all("SELECT * FROM audit_events")])
    finally:
        target.close()


def test_recovery_reissue_expiry_and_password_change_invalidate(local: Any) -> None:
    ctx, app, _, _, _ = local
    target, _, data = _account(local, "recoverable")
    try:
        uid = data["user"]["id"]
        old = _issue(local, uid).json()["token"]
        new = _issue(local, uid).json()["token"]
        with TestClient(app, base_url=ORIGIN) as fresh:
            assert _inspect(fresh, old).status_code == 422
            assert _inspect(fresh, new).status_code == 200
            ctx.db.execute("UPDATE password_recovery_tokens SET expires_at=? WHERE revoked_at IS NULL", (after(-1),))
            assert _inspect(fresh, new).status_code == 422
            newest = _issue(local, uid).json()["token"]
            reset_password(ctx, "recoverable", NEW_PASSWORD)
            assert _complete(fresh, newest).status_code == 422
    finally:
        target.close()


@pytest.mark.parametrize("mutation", ["disabled", "promoted", "issuer-demoted"])
def test_recovery_lifecycle_cannot_reactivate_or_reset_admin(local: Any, mutation: str) -> None:
    ctx, app, _, _, administrator = local
    target, _, data = _account(local, "recoverable")
    second, _, second_data = _account(local, "second", None)
    second.close()
    patch_user(ctx, second_data["user"]["id"], {"is_platform_admin": True}, actor=administrator["id"])
    try:
        uid = data["user"]["id"]
        token = _issue(local, uid).json()["token"]
        if mutation == "disabled":
            patch_user(ctx, uid, {"status": "disabled"}, actor=administrator["id"])
        elif mutation == "promoted":
            patch_user(ctx, uid, {"is_platform_admin": True}, actor=administrator["id"])
        else:
            patch_user(ctx, administrator["id"], {"is_platform_admin": False}, actor=second_data["user"]["id"])
        with TestClient(app, base_url=ORIGIN) as fresh:
            assert _complete(fresh, token).status_code == 422
        if mutation == "disabled":
            assert ctx.db.scalar("SELECT status FROM users WHERE id=?", (uid,)) == "disabled"
    finally:
        target.close()


def test_issue_refuses_admin_pending_disabled_and_nonadmin_issuer(local: Any) -> None:
    ctx, _, admin, headers, administrator = local
    pending = admin.post("/v1/users", json={"username": "pending"}, headers=headers).json()
    target, th, data = _account(local, "ordinary")
    try:
        assert _issue(local, administrator["id"]).status_code == 409
        assert _issue(local, pending["user"]["id"]).status_code == 409
        assert (
            target.post(
                f"/v1/users/{data['user']['id']}/recovery-token", json={"current_password": PASSWORD}, headers=th
            ).status_code
            == 403
        )
        patch_user(ctx, data["user"]["id"], {"status": "disabled"}, actor=administrator["id"])
        assert _issue(local, data["user"]["id"]).status_code == 409
    finally:
        target.close()


def test_concurrent_recovery_completes_once(local: Any) -> None:
    ctx, _, _, _, _ = local
    target, _, data = _account(local, "racing")
    target.close()
    token = _issue(local, data["user"]["id"]).json()["token"]
    barrier = Barrier(2)

    def complete_once():
        barrier.wait()
        try:
            recovery.complete(ctx, token, NEW_PASSWORD)
            return True
        except RagError:
            return False

    version = ctx.db.scalar("SELECT auth_version FROM users WHERE id=?", (data["user"]["id"],))
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: complete_once(), range(2))) == 1
    assert ctx.db.scalar("SELECT auth_version FROM users WHERE id=?", (data["user"]["id"],)) == version + 1


def test_recovery_issuance_rechecks_password_version_after_verification(
    local: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _, _, _, administrator = local
    target, _, data = _account(local, "recoverable")
    target.close()
    original = recovery.verify_password

    def changed_after_verification(encoded: str | None, password: str) -> bool:
        result = original(encoded, password)
        ctx.db.execute("UPDATE users SET auth_version=auth_version+1 WHERE id=?", (administrator["id"],))
        return result

    monkeypatch.setattr(recovery, "verify_password", changed_after_verification)
    assert _issue(local, data["user"]["id"]).status_code == 401
    assert ctx.db.scalar("SELECT count(*) FROM password_recovery_tokens") == 0


def test_ordinary_password_change_revokes_recovery_link(local: Any) -> None:
    ctx, app, _, _, _ = local
    target, th, data = _account(local, "recoverable")
    try:
        token = _issue(local, data["user"]["id"]).json()["token"]
        response = target.post(
            "/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}, headers=th
        )
        assert response.status_code == 204
        assert ctx.db.scalar("SELECT revoked_at FROM password_recovery_tokens") is not None
        with TestClient(app, base_url=ORIGIN) as fresh:
            assert _complete(fresh, token).status_code == 422
    finally:
        target.close()


def test_recovery_token_cannot_activate_or_bootstrap(local: Any) -> None:
    _, app, _, _, _ = local
    target, _, data = _account(local, "recoverable")
    target.close()
    token = _issue(local, data["user"]["id"]).json()["token"]
    with TestClient(app, base_url=ORIGIN) as fresh:
        for path in ("invitations/inspect", "bootstrap/inspect"):
            assert fresh.post(f"/v1/auth/{path}", json={"token": token}, headers={"Origin": ORIGIN}).status_code == 422
