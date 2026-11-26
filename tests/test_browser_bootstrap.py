"""Installation claims require local operator handoff, including under competing initializers."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ragcentre import cli
from ragcentre.accounts import bootstrap
from ragcentre.accounts.users import DEFAULT_TEAM_ID, create_user, init_admin, reset_password
from ragcentre.api.app import create_app
from ragcentre.context import open_context
from ragcentre.errors import Conflict, Forbidden, RagError, ValidationFailed
from ragcentre.settings import Settings
from ragcentre.timeutil import after
from test_local_auth_cli import operator_settings as operator_settings

PASSWORD = "a long installation password"
ORIGIN = "https://testserver"


@pytest.fixture
def uninitialized(tmp_path: Path):
    ctx = open_context(Settings(data_dir=tmp_path / "local", workers=0, auth_mode="local", browser_origin=ORIGIN))
    try:
        yield ctx
    finally:
        ctx.close()


def body(token: str) -> dict[str, str]:
    return {
        "token": token,
        "username": "administrator",
        "password": PASSWORD,
        "team_name": "My Team",
        "display_name": "Administrator",
    }


def test_claim_required_atomic_initialization_uses_default_team_and_persists_auth(uninitialized: Any) -> None:
    ctx = uninitialized
    claim = bootstrap.issue(ctx)
    with TestClient(create_app(ctx), base_url=ORIGIN) as client:
        assert client.get("/v1/corpora").status_code == 401
        assert (
            client.post("/v1/auth/bootstrap/complete", json=body("x" * 43), headers={"Origin": ORIGIN}).status_code
            == 422
        )
        for _ in range(2):
            info = client.post("/v1/auth/bootstrap/inspect", json={"token": claim["token"]}, headers={"Origin": ORIGIN})
            assert info.status_code == 200 and info.json()["team_id"] == DEFAULT_TEAM_ID
        response = client.post("/v1/auth/bootstrap/complete", json=body(claim["token"]), headers={"Origin": ORIGIN})
        assert response.status_code == 200, response.text
        current = response.json()["current"]
        assert current["user"]["is_platform_admin"] is True and current["memberships"][0]["role"] == "owner"
        assert response.json()["team_id"] == DEFAULT_TEAM_ID
        assert ctx.db.scalar("SELECT count(*) FROM teams") == 1
        assert ctx.db.scalar("SELECT name FROM teams WHERE id=?", (DEFAULT_TEAM_ID,)) == "My Team"
        assert ctx.db.scalar("SELECT local_enabled FROM auth_state") == 1
        assert client.post(
            "/v1/auth/bootstrap/complete", json=body(claim["token"]), headers={"Origin": ORIGIN}
        ).status_code in {409, 422}
        assert client.get("/v1/auth/config").json()["setup_required"] is False
    with pytest.raises(Conflict):
        bootstrap.issue(ctx)
    with pytest.raises(ValidationFailed):
        open_context(ctx.settings.with_(auth_mode="legacy"))


def test_expiry_reissue_and_cli_initializer_invalidate_claim(uninitialized: Any) -> None:
    ctx = uninitialized
    old = bootstrap.issue(ctx)["token"]
    new = bootstrap.issue(ctx)["token"]
    with pytest.raises(ValidationFailed):
        bootstrap.inspect(ctx, old)
    assert bootstrap.inspect(ctx, new)["team_id"] == DEFAULT_TEAM_ID
    ctx.db.execute("UPDATE installation_claims SET expires_at=? WHERE revoked_at IS NULL", (after(-1),))
    with pytest.raises(ValidationFailed):
        bootstrap.complete(ctx, new, "admin", PASSWORD, "", "Default")
    final = bootstrap.issue(ctx)["token"]
    init_admin(ctx, "cli-admin", PASSWORD)
    with pytest.raises(ValidationFailed):
        bootstrap.complete(ctx, final, "browser-admin", PASSWORD, "", "Default")
    assert ctx.db.scalar("SELECT count(*) FROM users") == 1


@pytest.mark.parametrize("cli_racer", [False, True])
def test_concurrent_initialization_has_one_admin_and_one_session_at_most(uninitialized: Any, cli_racer: bool) -> None:
    ctx = uninitialized
    claim = bootstrap.issue(ctx)["token"]
    barrier = Barrier(2)

    def initialize(index: int):
        barrier.wait()
        try:
            if index and cli_racer:
                init_admin(ctx, "cli-admin", PASSWORD)
            else:
                bootstrap.complete(ctx, claim, f"browser-{index}", PASSWORD, "", "Default")
            return True
        except RagError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(initialize, range(2))) == 1
    assert ctx.db.scalar("SELECT count(*) FROM users WHERE status='active' AND is_platform_admin=1") == 1
    assert ctx.db.scalar("SELECT count(*) FROM team_memberships") == 1
    assert ctx.db.scalar("SELECT count(*) FROM user_sessions") <= 1


def test_unrelated_existing_browser_session_blocks_bootstrap(uninitialized: Any) -> None:
    ctx = uninitialized
    created = create_user(ctx, "existing", "Existing", actor="operator")
    reset_password(ctx, "existing", PASSWORD)
    claim = bootstrap.issue(ctx)
    with TestClient(create_app(ctx), base_url=ORIGIN) as client:
        response = client.post(
            "/v1/auth/login", json={"username": "existing", "password": PASSWORD}, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 200
        response = client.post("/v1/auth/bootstrap/complete", json=body(claim["token"]), headers={"Origin": ORIGIN})
        assert response.status_code == 409
        assert client.get("/v1/auth/me").json()["user"]["id"] == created["user"]["id"]
    assert ctx.db.scalar("SELECT consumed_at FROM installation_claims") is None


@pytest.mark.parametrize("base_path", ["", "/console"])
def test_canonical_links_config_and_page_headers(uninitialized: Any, tmp_path: Path, base_path: str) -> None:
    ctx = uninitialized
    ctx.settings = ctx.settings.with_(browser_base_path=base_path)
    claim = bootstrap.issue(ctx)
    assert claim["link"].startswith(f"{ORIGIN}{base_path}/initialize#token=")
    assert "?" not in claim["link"]
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    (ui_dir / "index.html").write_text("<!doctype html><html><head></head><body>App</body></html>")
    with TestClient(create_app(ctx, ui_dir), base_url=ORIGIN) as client:
        config = client.get(f"{base_path}/v1/auth/config").json()
        assert config["browser_base_url"] == ORIGIN + base_path
        assert config["browser_base_path"] == base_path
        assert config["supported_flows"] == ["invitations", "recovery", "bootstrap"]
        response = client.get(f"{base_path}/initialize")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        response = client.post(
            f"{base_path}/v1/auth/bootstrap/inspect",
            json={"token": claim["token"]},
            headers={"Origin": ORIGIN, "X-Forwarded-Host": "evil.example"},
        )
        assert response.status_code == 200


@pytest.mark.parametrize("mode", [None, "legacy"])
def test_claim_issuance_requires_explicit_local_server_mode(uninitialized: Any, mode: str | None) -> None:
    ctx = uninitialized
    ctx.settings = ctx.settings.with_(auth_mode=mode)
    with pytest.raises(Forbidden):
        bootstrap.issue(ctx)


def test_setup_link_command_uses_local_database_settings_and_never_starts_services(
    operator_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAGCENTRE_AUTH_MODE", "local")
    monkeypatch.setenv("RAGCENTRE_BROWSER_ORIGIN", "https://canonical.example.test")
    monkeypatch.setenv("RAGCENTRE_BROWSER_BASE_PATH", "/console")
    result = CliRunner().invoke(cli.app, ["--json", "user", "setup-link"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["link"] == f"https://canonical.example.test/console/initialize#token={data['token']}"
    with closing(
        open_context(operator_settings.with_(auth_mode="local", browser_origin="https://canonical.example.test"))
    ) as ctx:
        assert ctx.db.scalar("SELECT count(*) FROM installation_claims") == 1
        assert ctx.db.scalar("SELECT token_hash FROM installation_claims") != data["token"]
        assert data["token"] not in str([dict(r) for r in ctx.db.all("SELECT * FROM audit_events")])
