from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.accounts.sessions import local_enabled
from ragcentre.accounts.users import (
    DEFAULT_TEAM_ID,
    complete_setup,
    create_user,
    init_admin,
    reset_password,
    set_membership,
)
from ragcentre.api.app import create_app
from ragcentre.api.permissions import POLICIES, SessionRoute
from ragcentre.api.routers import ROUTERS
from ragcentre.context import open_context
from ragcentre.errors import Conflict, ValidationFailed
from ragcentre.settings import Settings
from ragcentre.timeutil import after

PASSWORD = "correct horse battery staple"
ORIGIN = "https://testserver"
ADMIN = {"Authorization": "Bearer existing-admin"}


@pytest.fixture
def local(tmp_path: Path):
    ctx = open_context(
        Settings(data_dir=tmp_path, workers=0, api_token="existing-admin", auth_mode="local", browser_origin=ORIGIN)
    )
    admin = init_admin(ctx, "admin", PASSWORD)
    app = create_app(ctx)
    client = TestClient(app, base_url=ORIGIN)
    response = client.post(
        "/v1/auth/login", json={"username": "ADMIN", "password": PASSWORD}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf_token"]
    headers = {"Origin": ORIGIN, "X-RAGCentre-CSRF": csrf, "X-RAGCentre-Team-ID": DEFAULT_TEAM_ID}
    try:
        yield ctx, app, client, headers, admin
    finally:
        client.close()
        ctx.close()


def _account(local: Any, name: str, role: str | None = "viewer", team: str = DEFAULT_TEAM_ID):
    _ctx, app, admin, headers, _ = local
    created = admin.post("/v1/users", json={"username": name}, headers=headers)
    assert created.status_code == 201, created.text
    data = created.json()
    fresh = TestClient(app, base_url=ORIGIN)
    setup = fresh.post(
        "/v1/auth/setup", json={"token": data["setup_token"], "password": PASSWORD}, headers={"Origin": ORIGIN}
    )
    assert setup.status_code == 204, setup.text
    if role:
        r = admin.put(
            f"/v1/teams/{team}/members/{name}", json={"role": role}, headers={**headers, "X-RAGCentre-Team-ID": team}
        )
        assert r.status_code == 200, r.text
    login = fresh.post("/v1/auth/login", json={"username": name, "password": PASSWORD}, headers={"Origin": ORIGIN})
    assert login.status_code == 200, login.text
    return fresh, {"Origin": ORIGIN, "X-RAGCentre-CSRF": login.json()["csrf_token"], "X-RAGCentre-Team-ID": team}, data


def test_route_policy_covers_every_endpoint() -> None:
    schema = create_app(None).openapi()
    routes = {(method.upper(), path) for path, methods in schema["paths"].items() for method in methods}
    assert routes == set(POLICIES)
    assert all(isinstance(route, SessionRoute) for router in ROUTERS for route in router.routes)


def test_local_mode_never_grants_implicit_admin(tmp_path: Path) -> None:
    ctx = open_context(Settings(data_dir=tmp_path, workers=0, auth_mode="local"))
    try:
        with TestClient(create_app(ctx)) as client:
            assert client.get("/v1/corpora").status_code == 401
            assert client.get("/v1/auth/config").json() == {
                "mode": "local",
                "setup_required": True,
                "browser_base_url": None,
                "browser_base_path": "",
                "supported_flows": [],
            }
            assert client.get("/v1/health").json()["auth_required"] is True
    finally:
        ctx.close()


def test_account_setup_is_single_use_and_never_exposes_hash(local) -> None:
    ctx, _, admin, headers, _ = local
    fresh, _, data = _account(local, "reader")
    try:
        assert "password_hash" not in str(admin.get("/v1/users", headers=headers).json())
        assert ctx.db.scalar(
            "SELECT password_hash FROM local_credentials WHERE user_id=?", (data["user"]["id"],)
        ).startswith("$argon2id$")
        assert (
            ctx.db.scalar("SELECT token_hash FROM account_setup_tokens WHERE user_id=?", (data["user"]["id"],))
            != data["setup_token"]
        )
        assert (
            fresh.post(
                "/v1/auth/setup", json={"token": data["setup_token"], "password": PASSWORD}, headers={"Origin": ORIGIN}
            ).status_code
            == 422
        )
        assert admin.post("/v1/users", json={"username": "READER"}, headers=headers).status_code == 409
    finally:
        fresh.close()


def test_setup_reissue_invalidates_old_link(local) -> None:
    _, app, admin, headers, _ = local
    data = admin.post("/v1/users", json={"username": "pending"}, headers=headers).json()
    new = admin.post(f"/v1/users/{data['user']['id']}/setup-token", headers=headers).json()
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert (
            fresh.post(
                "/v1/auth/setup", json={"token": data["setup_token"], "password": PASSWORD}, headers={"Origin": ORIGIN}
            ).status_code
            == 422
        )
        assert (
            fresh.post(
                "/v1/auth/setup", json={"token": new["setup_token"], "password": PASSWORD}, headers={"Origin": ORIGIN}
            ).status_code
            == 204
        )


def test_session_cookie_and_csrf(local) -> None:
    _, app, admin, headers, _ = local
    assert "__Host-ragcentre_session" in admin.cookies
    assert admin.get("/v1/auth/me").headers["cache-control"] == "no-store"
    assert (
        admin.post("/v1/corpora", json={"name": "bad"}, headers={"X-RAGCentre-Team-ID": DEFAULT_TEAM_ID}).status_code
        == 403
    )
    assert (
        admin.post(
            "/v1/corpora", json={"name": "bad"}, headers={**headers, "Origin": "https://other.example"}
        ).status_code
        == 403
    )
    assert admin.post("/v1/corpora", json={"name": "ok"}, headers=headers).status_code == 201
    with TestClient(app, base_url=ORIGIN) as fresh:
        r = fresh.post("/v1/auth/login", json={"username": "admin", "password": PASSWORD}, headers={"Origin": ORIGIN})
        assert "Secure" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
        assert fresh.post("/v1/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 403


def test_invalid_bearer_does_not_fall_back_to_session(local) -> None:
    _, _, client, headers, _ = local
    assert client.get("/v1/corpora", headers={**headers, "Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/auth/me", headers=ADMIN).status_code == 403
    assert client.get("/v1/users", headers=ADMIN).status_code == 403


def test_viewer_lists_and_reads_but_cannot_mutate(local) -> None:
    _, _, admin, headers, _ = local
    corpus = admin.post("/v1/corpora", json={"name": "docs"}, headers=headers).json()
    doc = admin.post(
        f"/v1/corpora/{corpus['id']}/documents",
        files={"file": ("a.md", b"team document", "text/markdown")},
        headers=headers,
    ).json()
    viewer, vh, _ = _account(local, "viewer")
    try:
        assert viewer.get("/v1/corpora", headers=vh).json()["items"][0]["id"] == corpus["id"]
        assert viewer.get(f"/v1/corpora/{corpus['id']}/documents", headers=vh).status_code == 200
        assert viewer.get(f"/v1/corpora/{corpus['id']}/pipelines", headers=vh).status_code == 200
        assert viewer.get(f"/v1/documents/{doc['id']}/content", headers=vh).status_code == 200
        assert viewer.delete(f"/v1/documents/{doc['id']}", headers=vh).status_code == 403
        assert viewer.post("/v1/corpora", json={"name": "denied"}, headers=vh).status_code == 403
        assert viewer.get("/v1/providers", headers=vh).status_code == 403
        summaries = viewer.get("/v1/provider-summaries", headers=vh)
        assert summaries.status_code == 200
        assert "config" not in summaries.json()["items"][0]
        assert viewer.get(f"/v1/teams/{DEFAULT_TEAM_ID}/members", headers=vh).status_code == 403
    finally:
        viewer.close()


def test_editor_can_build_but_not_manage_sources_or_members(local) -> None:
    _, _, _, _, _ = local
    editor, eh, _ = _account(local, "editor", "editor")
    try:
        assert editor.post("/v1/corpora", json={"name": "edited"}, headers=eh).status_code == 201
        assert editor.get("/v1/providers", headers=eh).status_code == 200
        assert editor.get("/v1/provider-types", headers=eh).status_code == 200
        assert (
            editor.post(
                "/v1/providers", json={"name": "denied", "kind": "embedder", "type": "builtin_hash"}, headers=eh
            ).status_code
            == 403
        )
        assert (
            editor.post(
                f"/v1/teams/{DEFAULT_TEAM_ID}/tokens", json={"name": "no", "kind": "full"}, headers=eh
            ).status_code
            == 403
        )
    finally:
        editor.close()


def test_cross_team_ids_and_admin_selected_team(local) -> None:
    _, _, admin, headers, admin_user = local
    team = admin.post("/v1/teams", json={"name": "Other", "owner_user_id": admin_user["id"]}, headers=headers).json()
    other = {**headers, "X-RAGCentre-Team-ID": team["id"]}
    corpus = admin.post("/v1/corpora", json={"name": "other"}, headers=other).json()
    assert admin.get(f"/v1/corpora/{corpus['id']}", headers=headers).status_code == 404
    assert admin.get("/v1/corpora", headers=headers).json()["items"] == []
    viewer, vh, _ = _account(local, "isolated")
    try:
        assert viewer.get(f"/v1/corpora/{corpus['id']}", headers=vh).status_code == 404
        assert viewer.get("/v1/corpora", headers={**vh, "X-RAGCentre-Team-ID": team["id"]}).status_code == 404
    finally:
        viewer.close()


def test_removal_and_demotion_take_effect_next_request(local) -> None:
    _, _, admin, headers, _ = local
    viewer, vh, _ = _account(local, "removed", "editor")
    try:
        assert viewer.post("/v1/corpora", json={"name": "before"}, headers=vh).status_code == 201
        assert (
            admin.put(
                f"/v1/teams/{DEFAULT_TEAM_ID}/members/removed", json={"role": "viewer"}, headers=headers
            ).status_code
            == 200
        )
        assert viewer.post("/v1/corpora", json={"name": "after"}, headers=vh).status_code == 403
        assert admin.delete(f"/v1/teams/{DEFAULT_TEAM_ID}/members/removed", headers=headers).status_code == 204
        assert viewer.get("/v1/corpora", headers=vh).status_code == 404
        assert viewer.get("/v1/auth/me").json()["memberships"] == []
    finally:
        viewer.close()


def test_owner_and_admin_invariants(local) -> None:
    ctx, _, admin, headers, admin_user = local
    assert admin.delete(f"/v1/teams/{DEFAULT_TEAM_ID}/members/admin", headers=headers).status_code == 409
    assert (
        admin.patch(f"/v1/users/{admin_user['id']}", json={"is_platform_admin": False}, headers=headers).status_code
        == 409
    )
    assert admin.patch(f"/v1/users/{admin_user['id']}", json={"status": "disabled"}, headers=headers).status_code == 409
    assert ctx.db.scalar("SELECT status FROM users WHERE id=?", (admin_user["id"],)) == "active"


def test_password_change_and_disable_revoke_sessions(local) -> None:
    _, _, admin, headers, _ = local
    viewer, vh, data = _account(local, "passwords")
    try:
        changed = viewer.post(
            "/v1/auth/password", json={"current_password": PASSWORD, "password": PASSWORD + " new"}, headers=vh
        )
        assert changed.status_code == 204, changed.text
        assert viewer.get("/v1/auth/me").status_code == 401
        assert (
            viewer.post(
                "/v1/auth/login",
                json={"username": "passwords", "password": PASSWORD + " new"},
                headers={"Origin": ORIGIN},
            ).status_code
            == 200
        )
        assert (
            admin.patch(f"/v1/users/{data['user']['id']}", json={"status": "disabled"}, headers=headers).status_code
            == 200
        )
        assert viewer.get("/v1/auth/me").status_code == 401
    finally:
        viewer.close()


def test_session_idle_and_absolute_expiration(local) -> None:
    ctx, _, _, _, _ = local
    for field, value in (("last_seen_at", after(-9 * 3600)), ("expires_at", after(-1))):
        viewer, _, data = _account(local, "idle" if field == "last_seen_at" else "absolute")
        try:
            ctx.db.execute(f"UPDATE user_sessions SET {field}=? WHERE user_id=?", (value, data["user"]["id"]))
            assert viewer.get("/v1/auth/me").status_code == 401
        finally:
            viewer.close()


def test_setup_race_has_one_winner(local) -> None:
    ctx, _, _, _, admin_user = local
    data = create_user(ctx, "race", "Race", actor=admin_user["id"])

    def activate() -> bool:
        try:
            complete_setup(ctx, data["setup_token"], PASSWORD)
            return True
        except ValidationFailed:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: activate(), range(2))) == [False, True]


def test_final_owner_race_is_transactional(local) -> None:
    ctx, _, _, _, _ = local
    _account_client, _, _ = _account(local, "second", "owner")
    _account_client.close()

    def remove(name: str) -> bool:
        try:
            set_membership(ctx, DEFAULT_TEAM_ID, name, None, actor=None)
            return True
        except Conflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(remove, ["admin", "second"])) == [False, True]


def test_activation_latch_survives_restart(tmp_path: Path) -> None:
    ctx = open_context(Settings(data_dir=tmp_path, workers=0))
    init_admin(ctx, "admin", PASSWORD)
    ctx.close()
    reopened = open_context(Settings(data_dir=tmp_path, workers=0))
    try:
        assert local_enabled(reopened)
        with TestClient(create_app(reopened)) as client:
            assert client.get("/v1/corpora").status_code == 401
    finally:
        reopened.close()
    with pytest.raises(ValidationFailed, match="legacy mode"):
        open_context(Settings(data_dir=tmp_path, workers=0, auth_mode="legacy"))


def test_operator_password_recovery_revokes_sessions(local) -> None:
    ctx, _, client, _, _ = local
    reset_password(ctx, "admin", PASSWORD + " recovered")
    assert client.get("/v1/auth/me").status_code == 401


def test_admin_selected_team_cannot_ocr_check_other_provider(local) -> None:
    _ctx, _, admin, headers, admin_user = local
    team = admin.post("/v1/teams", json={"name": "Other", "owner_user_id": admin_user["id"]}, headers=headers).json()
    other = {**headers, "X-RAGCentre-Team-ID": team["id"]}
    provider = admin.post(
        "/v1/providers",
        json={"name": "b-parser", "kind": "parser", "type": "builtin_text", "config": {}},
        headers=other,
    )
    assert provider.status_code == 201, provider.text
    # A platform administrator selected in Default must not OCR-check another team's provider.
    assert admin.post(f"/v1/providers/{provider.json()['id']}/ocr-check", headers=headers).status_code == 404
    # Selected in the owning team, authorization passes (the 409 is the OCR layer rejecting a non-OCR parser).
    assert admin.post(f"/v1/providers/{provider.json()['id']}/ocr-check", headers=other).status_code == 409


def test_eval_run_with_other_teams_pipeline_is_404_without_name(local) -> None:
    ctx, _, admin, headers, admin_user = local
    team = admin.post("/v1/teams", json={"name": "Other", "owner_user_id": admin_user["id"]}, headers=headers).json()
    other = {**headers, "X-RAGCentre-Team-ID": team["id"]}
    corpus_a = admin.post("/v1/corpora", json={"name": "a"}, headers=headers).json()
    corpus_b = admin.post("/v1/corpora", json={"name": "b"}, headers=other).json()
    ctx.db.execute(
        "INSERT INTO pipelines (id, corpus_id, name, config, resolved, fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("p-a", corpus_a["id"], "local-pipeline", "{}", "{}", "fp-a", "now"),
    )
    ctx.db.execute(
        "INSERT INTO pipelines (id, corpus_id, name, config, resolved, fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("p-b", corpus_b["id"], "other-secret-pipeline", "{}", "{}", "fp-b", "now"),
    )
    ctx.db.execute(
        "INSERT INTO eval_datasets (id, corpus_id, name, created_at) VALUES (?, ?, ?, ?)",
        ("d-a", corpus_a["id"], "qa", "now"),
    )
    r = admin.post("/v1/eval/runs", json={"dataset_id": "d-a", "pipeline_ids": ["p-a", "p-b"]}, headers=headers)
    assert r.status_code == 404
    assert "other-secret-pipeline" not in r.text


def test_admin_selected_team_cannot_touch_cross_wired_source(local) -> None:
    ctx, _, admin, headers, admin_user = local
    team = admin.post("/v1/teams", json={"name": "Other", "owner_user_id": admin_user["id"]}, headers=headers).json()
    other = {**headers, "X-RAGCentre-Team-ID": team["id"]}
    corpus_a = admin.post("/v1/corpora", json={"name": "ca"}, headers=headers).json()
    provider_b = admin.post(
        "/v1/providers", json={"name": "b-s3", "kind": "connector", "type": "s3", "config": {}}, headers=other
    )
    assert provider_b.status_code == 201, provider_b.text
    # A pre-existing cross-wired source: Default's corpus points at Other's connector.
    ctx.db.execute(
        "INSERT INTO sync_sources (id, corpus_id, provider_id, name, scope, schedule_seconds, status, "
        "metadata_template, attributes_template, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'idle', '{}', '{}', 'now', 'now')",
        ("src-x", corpus_a["id"], provider_b.json()["id"], "x", "{}", 0),
    )
    # A platform administrator selected in Default cannot patch or sync it (provider cross-check).
    assert admin.patch("/v1/sources/src-x", json={"scope": {"bucket": "b"}}, headers=headers).status_code == 404
    assert admin.post("/v1/sources/src-x/sync", headers=headers).status_code == 404


def test_reenabling_disabled_pending_account_returns_to_pending(local) -> None:
    _, _, admin, headers, _ = local
    data = admin.post("/v1/users", json={"username": "pausable"}, headers=headers).json()
    uid = data["user"]["id"]
    assert admin.patch(f"/v1/users/{uid}", json={"status": "disabled"}, headers=headers).status_code == 200
    # Re-enabling an account that never set a password returns it to pending so a fresh link can be issued.
    assert admin.patch(f"/v1/users/{uid}", json={"status": "active"}, headers=headers).status_code == 200
    listed = admin.get("/v1/users", headers=headers).json()["items"]
    assert next(u for u in listed if u["id"] == uid)["status"] == "pending"
    reissued = admin.post(f"/v1/users/{uid}/setup-token", headers=headers)
    assert reissued.status_code == 200
    assert reissued.json()["setup_token"]
