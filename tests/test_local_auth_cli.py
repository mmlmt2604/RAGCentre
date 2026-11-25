"""Local operator commands and serve preflight, without a server or background services."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from click.testing import Result
from typer.testing import CliRunner

from ragcentre import cli
from ragcentre.accounts.passwords import verify_password
from ragcentre.accounts.users import DEFAULT_TEAM_ID, create_user, init_admin
from ragcentre.client import Client
from ragcentre.context import AppContext, open_context
from ragcentre.errors import ValidationFailed
from ragcentre.hashing import new_id
from ragcentre.logs import register_secret
from ragcentre.settings import Settings
from ragcentre.timeutil import now_iso

PASSWORD = "a long local account password"
NEW_PASSWORD = "a different recovery password"


@pytest.fixture(autouse=True)
def operator_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in tuple(os.environ):
        if key.startswith("RAGCENTRE_"):
            monkeypatch.delenv(key)
    settings = Settings(data_dir=tmp_path / "data", workers=0)
    monkeypatch.setenv("RAGCENTRE_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("RAGCENTRE_WORKERS", "0")

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Local operator commands must not use HTTP or start background services.")

    monkeypatch.setattr(AppContext, "start_workers", unexpected)
    monkeypatch.setattr(AppContext, "start_scheduler", unexpected)
    monkeypatch.setattr(cli.state, "client_factory", unexpected)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    return settings


def bootstrap(name: str = "admin", password: str = PASSWORD) -> Result:
    return CliRunner().invoke(cli.app, ["user", "init-admin"], input=f"{name}\n{password}\n{password}\n")


def assert_ok(result: Result) -> None:
    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"


def test_init_admin_migrates_activates_auth_and_owns_default_team(operator_settings: Settings) -> None:
    result = bootstrap("  Admin  ")
    assert_ok(result)
    assert "Initialized local administrator admin" in result.output
    assert PASSWORD not in result.output
    assert "Repeat for confirmation" in result.output
    with closing(open_context(operator_settings)) as ctx:
        user = ctx.db.one("SELECT * FROM users")
        assert user is not None
        assert (user["username"], user["status"], user["is_platform_admin"]) == ("admin", "active", 1)
        encoded = ctx.db.scalar("SELECT password_hash FROM local_credentials WHERE user_id=?", (user["id"],))
        assert encoded.startswith("$argon2id$")
        assert verify_password(encoded, PASSWORD)
        assert ctx.db.scalar("SELECT local_enabled FROM auth_state WHERE id=1") == 1
        assert (
            ctx.db.scalar(
                "SELECT role FROM team_memberships WHERE team_id=? AND user_id=?", (DEFAULT_TEAM_ID, user["id"])
            )
            == "owner"
        )
        event = ctx.db.one("SELECT actor_kind, action FROM audit_events")
        assert event is not None and tuple(event) == ("operator", "user.init-admin")
        assert PASSWORD not in str([dict(r) for r in ctx.db.all("SELECT * FROM audit_events")])


def test_init_admin_refuses_a_second_bootstrap(operator_settings: Settings) -> None:
    assert_ok(bootstrap())
    result = bootstrap("another-admin")
    assert result.exit_code == 1
    assert "error: conflict:" in result.output and "already exists" in result.output
    assert PASSWORD not in result.output
    with closing(open_context(operator_settings)) as ctx:
        assert ctx.db.scalar("SELECT count(*) FROM users") == 1
        assert ctx.db.scalar("SELECT count(*) FROM team_memberships") == 1


def test_password_confirmation_failure_does_not_open_database(operator_settings: Settings) -> None:
    result = CliRunner().invoke(cli.app, ["user", "init-admin"], input=f"admin\n{PASSWORD}\nwrong password\n")
    assert result.exit_code == 1
    assert PASSWORD not in result.output and "wrong password" not in result.output
    assert not operator_settings.data_dir.exists()


@pytest.mark.parametrize(
    "name,password,message", [("bad username", PASSWORD, "Usernames"), ("admin", "too short", "Passwords")]
)
def test_invalid_bootstrap_rolls_back(name: str, password: str, message: str, operator_settings: Settings) -> None:
    result = bootstrap(name, password)
    assert result.exit_code == 1
    assert "error: validation_error:" in result.output and message in result.output
    assert password not in result.output
    with closing(open_context(operator_settings)) as ctx:
        assert ctx.db.scalar("SELECT count(*) FROM users") == 0
        assert ctx.db.scalar("SELECT local_enabled FROM auth_state WHERE id=1") == 0


def test_reset_password_revokes_live_sessions(operator_settings: Settings) -> None:
    assert_ok(bootstrap())
    with closing(open_context(operator_settings)) as ctx:
        token, _ = ctx.sessions.login("admin", PASSWORD, "127.0.0.1")
        assert ctx.sessions.resolve(token) is not None
        version = ctx.db.scalar("SELECT auth_version FROM users WHERE username='admin'")
        result = CliRunner().invoke(
            cli.app, ["user", "reset-password", "ADMIN"], input=f"{NEW_PASSWORD}\n{NEW_PASSWORD}\n"
        )
        assert_ok(result)
        assert "sessions were revoked" in result.output
        assert PASSWORD not in result.output and NEW_PASSWORD not in result.output
        assert ctx.sessions.resolve(token) is None
        assert ctx.db.scalar("SELECT auth_version FROM users WHERE username='admin'") == version + 1
        encoded = ctx.db.scalar("SELECT password_hash FROM local_credentials")
        assert verify_password(encoded, NEW_PASSWORD)
        assert not verify_password(encoded, PASSWORD)
        assert ctx.db.scalar("SELECT actor_kind FROM audit_events WHERE action='user.password-recovery'") == "operator"


def test_reset_password_recovers_pending_account_and_invalidates_setup(operator_settings: Settings) -> None:
    assert_ok(bootstrap())
    with closing(open_context(operator_settings)) as ctx:
        created = create_user(ctx, "member", "Member", actor=ctx.db.scalar("SELECT id FROM users"))
        result = CliRunner().invoke(
            cli.app, ["user", "reset-password", "member"], input=f"{NEW_PASSWORD}\n{NEW_PASSWORD}\n"
        )
        assert_ok(result)
        assert ctx.db.scalar("SELECT status FROM users WHERE username='member'") == "active"
        assert ctx.db.scalar("SELECT count(*) FROM account_setup_tokens") == 0
        assert created["setup_token"] not in result.output


@pytest.mark.parametrize("command", [["user", "init-admin"], ["user", "reset-password", "admin"]])
def test_password_is_not_a_cli_option(command: list[str], operator_settings: Settings) -> None:
    result = CliRunner().invoke(cli.app, [*command, "--password", "not-a-password-option"])
    assert result.exit_code == 2
    assert "No such option" in result.output
    assert not operator_settings.data_dir.exists()


@pytest.mark.parametrize("reference", ["id", "slug", "name"])
def test_set_owner_resolves_local_team(reference: str, operator_settings: Settings) -> None:
    assert_ok(bootstrap())
    team = {"id": new_id(), "slug": "recovery-team", "name": "Recovery Team"}
    with closing(open_context(operator_settings)) as ctx:
        ctx.db.execute("INSERT INTO teams VALUES (?, ?, ?, ?)", (team["id"], team["name"], team["slug"], now_iso()))
    result = CliRunner().invoke(cli.app, ["--json", "team", "set-owner", team[reference], "ADMIN"])
    assert_ok(result)
    assert json.loads(result.output) == {"team_id": team["id"], "username": "admin", "role": "owner"}
    with closing(open_context(operator_settings)) as ctx:
        assert ctx.db.scalar("SELECT role FROM team_memberships WHERE team_id=?", (team["id"],)) == "owner"
        assert ctx.db.scalar("SELECT role FROM team_memberships WHERE team_id=?", (DEFAULT_TEAM_ID,)) == "owner"
        event = ctx.db.one("SELECT actor_kind, actor_id, team_id FROM audit_events WHERE action='membership.set'")
        assert event is not None and tuple(event) == ("operator", None, team["id"])


def test_set_owner_rejects_ambiguous_names(operator_settings: Settings) -> None:
    assert_ok(bootstrap())
    with closing(open_context(operator_settings)) as ctx:
        for slug in ("duplicate-a", "duplicate-b"):
            ctx.db.execute("INSERT INTO teams VALUES (?, ?, ?, ?)", (new_id(), "Duplicate", slug, now_iso()))
    result = CliRunner().invoke(cli.app, ["team", "set-owner", "Duplicate", "admin"])
    assert result.exit_code == 1
    assert "Several teams" in result.output and "id or slug" in result.output
    with closing(open_context(operator_settings)) as ctx:
        assert ctx.db.scalar("SELECT count(*) FROM team_memberships") == 1
    assert_ok(CliRunner().invoke(cli.app, ["team", "set-owner", "duplicate-a", "admin"]))


@pytest.mark.parametrize(
    "command,input_text",
    [
        (["team", "set-owner", "missing", "admin"], None),
        (["team", "set-owner", "default", "missing"], None),
        (["user", "reset-password", "missing"], f"{NEW_PASSWORD}\n{NEW_PASSWORD}\n"),
    ],
)
def test_unknown_team_or_user_is_a_clean_error(command: list[str], input_text: str | None) -> None:
    result = CliRunner().invoke(cli.app, command, input=input_text)
    assert result.exit_code == 1
    assert "error: not_found:" in result.output
    assert "Traceback" not in result.output and NEW_PASSWORD not in result.output


def test_operator_errors_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    from ragcentre.accounts import users

    sensitive = "operator-error-secret-value"
    register_secret(sensitive)

    def reject(*args: Any) -> Any:
        raise ValidationFailed(f"Rejected {sensitive}.")

    monkeypatch.setattr(users, "init_admin", reject)
    result = bootstrap()
    assert result.exit_code == 1
    assert "error: validation_error: Rejected [REDACTED]." in result.output
    assert sensitive not in result.output and PASSWORD not in result.output


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(AppContext, "start_workers", lambda self, count: calls.append(("workers", count)))
    monkeypatch.setattr(AppContext, "start_scheduler", lambda self: calls.append(("scheduler", True)))
    monkeypatch.setattr(uvicorn.Server, "run", lambda self: calls.append(("server", self.config.host)))
    return calls


@pytest.mark.parametrize("latched", [False, True])
def test_local_public_serve_accepts_https_origin_without_api_token(
    latched: bool, monkeypatch: pytest.MonkeyPatch, served: list[tuple[str, object]]
) -> None:
    if latched:
        assert_ok(bootstrap())
    else:
        monkeypatch.setenv("RAGCENTRE_AUTH_MODE", "local")
    monkeypatch.setenv("RAGCENTRE_BROWSER_ORIGIN", "https://ragcentre.example.test")
    assert_ok(CliRunner().invoke(cli.app, ["serve", "--host", "0.0.0.0"]))
    assert served == [("workers", 0), ("scheduler", True), ("server", "0.0.0.0")]


@pytest.mark.parametrize("latched", [False, True])
def test_allow_no_token_cannot_override_local_auth(
    latched: bool, monkeypatch: pytest.MonkeyPatch, operator_settings: Settings
) -> None:
    if latched:
        assert_ok(bootstrap())
    else:
        monkeypatch.setenv("RAGCENTRE_AUTH_MODE", "local")
    result = CliRunner().invoke(cli.app, ["serve", "--allow-no-token"])
    assert result.exit_code == 2
    assert "--allow-no-token" in result.output and "legacy" in result.output
    if not latched:
        assert not operator_settings.data_dir.exists()


@pytest.mark.parametrize(
    "origin,insecure,message",
    [
        (None, False, "RAGCENTRE_BROWSER_ORIGIN"),
        ("http://ragcentre.example.test", False, "HTTPS"),
        ("https://ragcentre.example.test", True, "loopback"),
    ],
)
def test_local_public_serve_validates_browser_settings_before_creating_data(
    origin: str | None, insecure: bool, message: str, monkeypatch: pytest.MonkeyPatch, operator_settings: Settings
) -> None:
    monkeypatch.setenv("RAGCENTRE_AUTH_MODE", "local")
    if origin:
        monkeypatch.setenv("RAGCENTRE_BROWSER_ORIGIN", origin)
    monkeypatch.setenv("RAGCENTRE_INSECURE_LOCAL_COOKIE", str(insecure).lower())
    result = CliRunner().invoke(cli.app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "error: validation_error:" in result.output and message in result.output
    assert not operator_settings.data_dir.exists()


def test_explicit_legacy_cannot_override_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    assert_ok(bootstrap())
    monkeypatch.setenv("RAGCENTRE_AUTH_MODE", "legacy")
    result = CliRunner().invoke(cli.app, ["serve", "--allow-no-token"])
    assert result.exit_code == 1
    assert "legacy mode cannot disable authentication" in result.output


def test_legacy_public_serve_rejected_without_creating_data(operator_settings: Settings) -> None:
    result = CliRunner().invoke(cli.app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "RAGCENTRE_API_TOKEN" in result.output
    assert not operator_settings.data_dir.exists()


def test_legacy_public_preflight_does_not_migrate_existing_database(operator_settings: Settings) -> None:
    operator_settings.data_dir.mkdir()
    with closing(sqlite3.connect(operator_settings.db_path)) as conn:
        conn.execute("CREATE TABLE existing_data (value TEXT)")
    result = CliRunner().invoke(cli.app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert not operator_settings.cache_path.exists()
    with closing(sqlite3.connect(operator_settings.db_path)) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("existing_data",)]


def test_persisted_activation_is_visible_in_live_wal_and_encoded_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[tuple[str, object]]
) -> None:
    settings = Settings(data_dir=tmp_path / "data ?#")
    monkeypatch.setenv("RAGCENTRE_DATA_DIR", str(settings.data_dir))
    with closing(open_context(settings)) as ctx:
        init_admin(ctx, "admin", PASSWORD)
        monkeypatch.setenv("RAGCENTRE_BROWSER_ORIGIN", "https://ragcentre.example.test")
        assert_ok(CliRunner().invoke(cli.app, ["serve", "--host", "0.0.0.0"]))
    assert ("server", "0.0.0.0") in served


def test_unreadable_auth_state_fails_closed(operator_settings: Settings) -> None:
    operator_settings.data_dir.mkdir()
    operator_settings.db_path.write_text("not a sqlite database")
    result = CliRunner().invoke(cli.app, ["serve", "--host", "0.0.0.0", "--allow-no-token"])
    assert result.exit_code == 1
    assert "Cannot read the local database authentication mode" in result.output
    assert not operator_settings.cache_path.exists()


def test_resource_commands_still_use_bearer_http(monkeypatch: pytest.MonkeyPatch, operator_settings: Settings) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    monkeypatch.setenv("RAGCENTRE_API_TOKEN", "existing-resource-token")
    monkeypatch.setattr(
        cli.state, "client_factory", lambda: Client(http=httpx.Client(transport=httpx.MockTransport(respond)))
    )
    assert_ok(CliRunner().invoke(cli.app, ["corpus", "list"]))
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/corpora"
    assert requests[0].headers["authorization"] == "Bearer existing-resource-token"
    assert "cookie" not in requests[0].headers
    assert not operator_settings.data_dir.exists()
