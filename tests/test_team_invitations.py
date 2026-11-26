"""Real SQLite invitation lifecycle, transaction and isolation coverage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.accounts import invitations
from ragcentre.accounts.users import DEFAULT_TEAM_ID, create_user, patch_user, reissue_setup, set_membership
from ragcentre.errors import Conflict, RagError, ValidationFailed
from ragcentre.timeutil import after
from test_local_accounts import ORIGIN, PASSWORD, _account
from test_local_accounts import local as local


def issue(local: Any, name: str = "invitee", role: str = "viewer") -> dict[str, Any]:
    _, _, admin, headers, _ = local
    response = admin.post(
        f"/v1/teams/{DEFAULT_TEAM_ID}/invitations", json={"username": name, "role": role}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def inspect(client: TestClient, token: str):
    return client.post("/v1/auth/invitations/inspect", json={"token": token}, headers={"Origin": ORIGIN})


def activate(client: TestClient, token: str):
    return client.post(
        "/v1/auth/invitations/activate", json={"token": token, "password": PASSWORD}, headers={"Origin": ORIGIN}
    )


def test_invitation_expiry_uses_server_clock_at_exact_deadline(local: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from ragcentre import timeutil

    ctx, _, _, _, _ = local
    created = issue(local)
    assert invitations.inspect(ctx, created["token"])["mode"] == "activate"
    deadline = timeutil.parse_iso(created["expires_at"])
    monkeypatch.setattr(timeutil, "utcnow", lambda: deadline)
    with pytest.raises(ValidationFailed):
        invitations.inspect(ctx, created["token"])
    with ctx.db.read() as conn:
        assert invitations.invitation_out(conn, created["invitation"]["id"], DEFAULT_TEAM_ID)["status"] == "expired"


def test_failed_invitation_insert_rolls_back_username_reservation(local: Any) -> None:
    import sqlite3

    ctx, _, _, _, administrator = local
    ctx.db.execute(
        "CREATE TRIGGER fail_invitation BEFORE INSERT ON team_invitations BEGIN SELECT RAISE(ABORT, 'simulated insert failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        invitations.issue(ctx, DEFAULT_TEAM_ID, "rollback", "", "viewer", actor=administrator["id"])
    assert ctx.db.scalar("SELECT count(*) FROM users WHERE username='rollback'") == 0
    assert ctx.db.scalar("SELECT count(*) FROM team_invitations") == 0


def test_reservation_inspection_atomic_activation_and_replay(local: Any) -> None:
    ctx, app, admin, headers, _ = local
    created = issue(local)
    row = created["invitation"]
    assert row["kind"] == "new_account" and row["role"] == "viewer" and row["status"] == "pending"
    assert created["link"] == f"{ORIGIN}/join#token={created['token']}" and created["path"] == "/join"
    assert ctx.db.scalar("SELECT count(*) FROM team_memberships WHERE user_id=?", (row["user_id"],)) == 0
    assert ctx.db.scalar("SELECT count(*) FROM local_credentials WHERE user_id=?", (row["user_id"],)) == 0
    assert ctx.db.scalar("SELECT count(*) FROM account_setup_tokens WHERE user_id=?", (row["user_id"],)) == 0
    assert created["token"] not in str([dict(r) for r in ctx.db.all("SELECT * FROM audit_events")])
    assert ctx.db.scalar("SELECT token_hash FROM team_invitations") != created["token"]
    with TestClient(app, base_url=ORIGIN) as fresh:
        result = inspect(fresh, created["token"])
        assert result.status_code == 200 and result.json()["mode"] == "activate"
        assert inspect(fresh, created["token"]).status_code == 200
        assert set(result.json()) == {"username", "display_name", "team_id", "team_name", "role", "expires_at", "mode"}
        response = activate(fresh, created["token"])
        assert response.status_code == 200, response.text
        arrival = response.json()
        assert arrival["team_id"] == DEFAULT_TEAM_ID and arrival["current"]["user"]["status"] == "active"
        assert arrival["current"]["memberships"][0]["role"] == "viewer"
        assert "Secure" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
        assert fresh.get("/v1/auth/me").status_code == 200
        assert activate(fresh, created["token"]).status_code == 422
        assert inspect(fresh, created["token"]).status_code == 422
    listed = admin.get(f"/v1/teams/{DEFAULT_TEAM_ID}/invitations", headers=headers).json()["items"]
    assert listed[0]["status"] == "accepted" and "token" not in listed[0] and "token_hash" not in listed[0]


def test_wrong_browser_identity_never_replaces_session(local: Any) -> None:
    _, _, admin, _, _ = local
    created = issue(local)
    assert activate(admin, created["token"]).status_code == 409
    assert admin.get("/v1/auth/me").json()["user"]["username"] == "admin"
    assert inspect(admin, created["token"]).status_code == 200


def test_existing_identity_accept_without_selected_team_and_no_role_overwrite(local: Any) -> None:
    ctx, app, admin, _, _ = local
    existing, eh, data = _account(local, "existing", None)
    try:
        created = issue(local, "existing", "owner")
        assert created["invitation"]["kind"] == "existing_account"
        with TestClient(app, base_url=ORIGIN) as fresh:
            assert activate(fresh, created["token"]).status_code == 422
        assert inspect(existing, created["token"]).json()["mode"] == "sign_in"
        wrong = admin.post(
            "/v1/auth/invitations/accept",
            json={"token": created["token"]},
            headers={k: v for k, v in eh.items() if k != "X-RAGCentre-Team-ID"},
        )
        assert wrong.status_code == 403
        set_membership(ctx, DEFAULT_TEAM_ID, "existing", "viewer", actor=local[4]["id"])
        response = existing.post(
            "/v1/auth/invitations/accept",
            json={"token": created["token"]},
            headers={k: v for k, v in eh.items() if k != "X-RAGCentre-Team-ID"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["already_member"] is True
        assert ctx.db.scalar("SELECT role FROM team_memberships WHERE user_id=?", (data["user"]["id"],)) == "viewer"
        assert response.json()["current"]["memberships"][0]["role"] == "viewer"
    finally:
        existing.close()


def test_existing_account_accept_grants_membership_and_wrong_identity_does_not_consume(local: Any) -> None:
    ctx, _, admin, headers, _ = local
    existing, eh, data = _account(local, "unjoined", None)
    try:
        created = issue(local, "unjoined", "editor")
        assert (
            admin.post("/v1/auth/invitations/accept", json={"token": created["token"]}, headers=headers).status_code
            == 403
        )
        assert ctx.db.scalar("SELECT accepted_at FROM team_invitations") is None
        response = existing.post(
            "/v1/auth/invitations/accept",
            json={"token": created["token"]},
            headers={"Origin": ORIGIN, "X-RAGCentre-CSRF": eh["X-RAGCentre-CSRF"]},
        )
        assert response.status_code == 200 and response.json()["already_member"] is False
        assert ctx.db.scalar("SELECT role FROM team_memberships WHERE user_id=?", (data["user"]["id"],)) == "editor"
    finally:
        existing.close()


def test_reissue_revoke_expiry_and_team_scoping(local: Any) -> None:
    ctx, app, admin, headers, owner = local
    created = issue(local)
    iid = created["invitation"]["id"]
    reissued = admin.post(f"/v1/teams/{DEFAULT_TEAM_ID}/invitations/{iid}/reissue", headers=headers)
    assert reissued.status_code == 200, reissued.text
    replacement = reissued.json()
    assert replacement["invitation"]["id"] == iid and replacement["invitation"]["generation"] == 2
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert inspect(fresh, created["token"]).status_code == 422
        assert inspect(fresh, replacement["token"]).status_code == 200
        ctx.db.execute("UPDATE team_invitations SET expires_at=? WHERE id=?", (after(-1), iid))
        assert inspect(fresh, replacement["token"]).status_code == 422
    other = admin.post("/v1/teams", json={"name": "Other", "owner_user_id": owner["id"]}, headers=headers).json()["id"]
    oh = {**headers, "X-RAGCentre-Team-ID": other}
    assert admin.post(f"/v1/teams/{other}/invitations/{iid}/reissue", headers=oh).status_code == 404
    assert admin.delete(f"/v1/teams/{other}/invitations/{iid}", headers=oh).status_code == 404
    assert admin.get(f"/v1/teams/{other}/invitations", headers=headers).status_code == 404
    assert admin.delete(f"/v1/teams/{DEFAULT_TEAM_ID}/invitations/{iid}", headers=headers).status_code == 204
    assert ctx.db.scalar("SELECT status FROM users WHERE id=?", (created["invitation"]["user_id"],)) == "pending"


def test_owner_cannot_adopt_preexisting_pending_or_disabled_accounts(local: Any) -> None:
    ctx, _, admin, headers, administrator = local
    owner, oh, _ = _account(local, "owner", "owner")
    try:
        pending = create_user(ctx, "privileged", "Private", actor=administrator["id"])
        patch_user(ctx, pending["user"]["id"], {"is_platform_admin": True}, actor=administrator["id"])
        for name in ("privileged", "admin"):
            response = owner.post(f"/v1/teams/{DEFAULT_TEAM_ID}/invitations", json={"username": name}, headers=oh)
            assert response.status_code == 409
            assert "is_platform_admin" not in response.text and "token" not in response.json()
        created = owner.post(
            f"/v1/teams/{DEFAULT_TEAM_ID}/invitations", json={"username": "reserved"}, headers=oh
        ).json()
        assert created["invitation"]["kind"] == "new_account"
        iid = created["invitation"]["id"]
        reissue_setup(ctx, created["invitation"]["user_id"], actor=administrator["id"])
        assert owner.post(f"/v1/teams/{DEFAULT_TEAM_ID}/invitations/{iid}/reissue", headers=oh).status_code == 409
        assert inspect(admin, created["token"]).status_code == 422
        assert admin.get(f"/v1/teams/{DEFAULT_TEAM_ID}/invitations", headers=headers).status_code == 200
    finally:
        owner.close()


def test_owner_demotion_permanently_revokes_authority_and_admin_can_reissue(local: Any) -> None:
    ctx, app, _, _, administrator = local
    owner, oh, _ = _account(local, "owner", "owner")
    try:
        response = owner.post(f"/v1/teams/{DEFAULT_TEAM_ID}/invitations", json={"username": "reserved"}, headers=oh)
        assert response.status_code == 201
        created = response.json()
        set_membership(ctx, DEFAULT_TEAM_ID, "owner", "viewer", actor=administrator["id"])
        set_membership(ctx, DEFAULT_TEAM_ID, "owner", "owner", actor=administrator["id"])
        with TestClient(app, base_url=ORIGIN) as fresh:
            assert inspect(fresh, created["token"]).status_code == 422
            reissued = invitations.reissue(ctx, DEFAULT_TEAM_ID, created["invitation"]["id"], actor=administrator["id"])
            assert activate(fresh, reissued["token"]).status_code == 200
    finally:
        owner.close()


@pytest.mark.parametrize("mutation", ["membership", "credentials", "platform", "disable"])
def test_reserved_target_lifecycle_prevents_activation(local: Any, mutation: str) -> None:
    ctx, app, _, _, administrator = local
    created = issue(local)
    uid = created["invitation"]["user_id"]
    if mutation == "membership":
        set_membership(ctx, DEFAULT_TEAM_ID, "invitee", "viewer", actor=administrator["id"])
    elif mutation == "credentials":
        reissue_setup(ctx, uid, actor=administrator["id"])
    elif mutation == "platform":
        patch_user(ctx, uid, {"is_platform_admin": True}, actor=administrator["id"])
    else:
        patch_user(ctx, uid, {"status": "disabled"}, actor=administrator["id"])
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert activate(fresh, created["token"]).status_code == 422
    assert ctx.db.scalar("SELECT count(*) FROM local_credentials WHERE user_id=?", (uid,)) == 0


def test_concurrent_issue_reserves_one_account_and_one_invitation(local: Any) -> None:
    ctx, _, _, _, administrator = local
    barrier = Barrier(2)

    def create():
        barrier.wait()
        try:
            return invitations.issue(ctx, DEFAULT_TEAM_ID, "racing", "", "viewer", actor=administrator["id"])
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert ctx.db.scalar("SELECT count(*) FROM users WHERE username='racing'") == 1
    assert ctx.db.scalar("SELECT count(*) FROM team_invitations") == 1


def test_concurrent_activation_commits_one_session_and_one_membership(local: Any) -> None:
    ctx, _, _, _, _ = local
    created = issue(local)
    barrier = Barrier(2)

    def accept_once():
        barrier.wait()
        try:
            return invitations.activate(ctx, created["token"], PASSWORD)
        except RagError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: accept_once(), range(2)))
    assert sum(r is not None for r in results) == 1
    uid = created["invitation"]["user_id"]
    assert ctx.db.scalar("SELECT count(*) FROM user_sessions WHERE user_id=?", (uid,)) == 1
    assert ctx.db.scalar("SELECT count(*) FROM team_memberships WHERE user_id=?", (uid,)) == 1


def test_failed_session_insert_rolls_back_entire_activation(local: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _, _, _, _ = local
    created = issue(local)

    def fail(*args: Any) -> None:
        raise ValidationFailed("Simulated session insertion failure")

    monkeypatch.setattr(invitations, "insert_session", fail)
    with pytest.raises(ValidationFailed):
        invitations.activate(ctx, created["token"], PASSWORD)
    uid = created["invitation"]["user_id"]
    assert ctx.db.scalar("SELECT status FROM users WHERE id=?", (uid,)) == "pending"
    assert ctx.db.scalar("SELECT count(*) FROM local_credentials WHERE user_id=?", (uid,)) == 0
    assert ctx.db.scalar("SELECT count(*) FROM team_memberships WHERE user_id=?", (uid,)) == 0
    assert invitations.inspect(ctx, created["token"])["mode"] == "activate"


def test_public_guards_purpose_separation_and_human_only_protected_routes(local: Any) -> None:
    _, app, admin, headers, administrator = local
    created = issue(local)
    token = created["token"]
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert fresh.post("/v1/auth/invitations/inspect", json={"token": token}).status_code == 403
        assert (
            fresh.post(
                "/v1/auth/invitations/inspect", json={"token": token}, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            fresh.post(
                "/v1/auth/invitations/inspect", content="{}", headers={"Origin": ORIGIN, "Content-Type": "text/plain"}
            ).status_code
            == 415
        )
        assert (
            fresh.post(
                "/v1/auth/invitations/inspect",
                content="x" * 17000,
                headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            ).status_code
            == 413
        )
        for path in ("recovery/inspect", "bootstrap/inspect"):
            assert fresh.post(f"/v1/auth/{path}", json={"token": token}, headers={"Origin": ORIGIN}).status_code == 422
        assert (
            fresh.post(
                "/v1/auth/setup", json={"token": token, "password": PASSWORD}, headers={"Origin": ORIGIN}
            ).status_code
            == 422
        )
        for path in ("/v1/auth/invitations/inspect", "/v1/auth/invitations/inspect/extra"):
            status = fresh.post(
                path, json={"token": token}, headers={"Origin": ORIGIN, "Authorization": "Bearer invalid"}
            ).status_code
            assert status in {401, 403}
    token_headers = {"Authorization": "Bearer existing-admin", "X-RAGCentre-Team-ID": DEFAULT_TEAM_ID}
    assert admin.get(f"/v1/teams/{DEFAULT_TEAM_ID}/invitations", headers=token_headers).status_code == 403
    assert (
        admin.post(
            f"/v1/teams/{DEFAULT_TEAM_ID}/invitations", json={"username": "blocked"}, headers=token_headers
        ).status_code
        == 403
    )
    assert (
        admin.post(
            f"/v1/users/{administrator['id']}/recovery-token",
            json={"current_password": PASSWORD},
            headers=token_headers,
        ).status_code
        == 403
    )
    assert admin.post("/v1/auth/invitations/accept", json={"token": token}).status_code == 403
    response = admin.post(
        f"/v1/teams/{DEFAULT_TEAM_ID}/invitations",
        json={"username": "safe", "is_platform_admin": True},
        headers=headers,
    )
    assert response.status_code == 422 and token not in response.text


def test_bearer_modes_cannot_reuse_cookie_authority_or_parse_new_protected_bodies(local: Any) -> None:
    _, _, admin, headers, _ = local
    created = issue(local)
    for kind in ("full", "read"):
        issued = admin.post(f"/v1/teams/{DEFAULT_TEAM_ID}/tokens", json={"name": kind, "kind": kind}, headers=headers)
        assert issued.status_code == 201
        auth = {**headers, "Authorization": "Bearer " + issued.json()["token"]}
        assert admin.get(f"/v1/teams/{DEFAULT_TEAM_ID}/invitations", headers=auth).status_code == 403
        assert (
            admin.post(
                f"/v1/teams/{DEFAULT_TEAM_ID}/invitations",
                content="{",
                headers={**auth, "Content-Type": "application/json"},
            ).status_code
            == 403
        )
        assert (
            admin.post("/v1/auth/invitations/inspect", json={"token": created["token"]}, headers=auth).status_code
            == 403
        )
    mixed = {**headers, "Authorization": "Bearer wrong"}
    assert admin.post("/v1/auth/invitations/accept", json={"token": created["token"]}, headers=mixed).status_code == 401
    assert (
        admin.post(
            f"/v1/teams/{DEFAULT_TEAM_ID}/invitations",
            content="{",
            headers={**mixed, "Content-Type": "application/json"},
        ).status_code
        == 401
    )
    response = admin.post(
        "/v1/auth/invitations/inspect", json={"token": created["token"] * 10}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 422 and created["token"] not in response.text


@pytest.mark.parametrize("change", ["target", "reissue"])
def test_acceptance_rechecks_after_password_hashing(local: Any, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    ctx, _, _, _, administrator = local
    created = issue(local)
    original = invitations.hash_password

    def changed_while_hashing(password: str) -> str:
        encoded = original(password)
        if change == "target":
            set_membership(ctx, DEFAULT_TEAM_ID, "invitee", "viewer", actor=administrator["id"])
        else:
            invitations.reissue(ctx, DEFAULT_TEAM_ID, created["invitation"]["id"], actor=administrator["id"])
        return encoded

    monkeypatch.setattr(invitations, "hash_password", changed_while_hashing)
    with pytest.raises(ValidationFailed):
        invitations.activate(ctx, created["token"], PASSWORD)
    assert (
        ctx.db.scalar("SELECT count(*) FROM local_credentials WHERE user_id=?", (created["invitation"]["user_id"],))
        == 0
    )
