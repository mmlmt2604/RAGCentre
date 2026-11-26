"""Operator-issued installation claims: browser access alone never authorizes setup."""

from __future__ import annotations

import secrets
import sqlite3
from typing import TYPE_CHECKING

from ragcentre.accounts.credentials import link_metadata
from ragcentre.accounts.http import validate_browser_settings
from ragcentre.accounts.passwords import hash_password
from ragcentre.accounts.sessions import insert_session, local_enabled
from ragcentre.accounts.users import DEFAULT_TEAM_ID, audit, username
from ragcentre.errors import Conflict, Forbidden, ValidationFailed
from ragcentre.hashing import new_id, sha256_text
from ragcentre.timeutil import after, now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext

INVALID = "The installation link is invalid or no longer usable."


def _settings(ctx: AppContext) -> None:
    if ctx.settings.auth_mode != "local" or not local_enabled(ctx):
        raise Forbidden(
            "Configure and restart the server with RAGCENTRE_AUTH_MODE=local before issuing or using an installation link."
        )
    validate_browser_settings(ctx.settings)
    if ctx.settings.browser_origin is None:
        raise ValidationFailed("Set RAGCENTRE_BROWSER_ORIGIN before issuing browser links.")


def issue(ctx: AppContext) -> dict[str, str]:
    _settings(ctx)
    token, claim_id, now, expires_at = secrets.token_urlsafe(32), new_id(), now_iso(), after(900)
    metadata = link_metadata(ctx.settings, "initialize", token)
    with ctx.db.write() as conn:
        if conn.execute("SELECT 1 FROM users WHERE status='active' AND is_platform_admin=1").fetchone():
            raise Conflict("An active platform administrator already exists.")
        conn.execute(
            "UPDATE installation_claims SET revoked_at=? WHERE consumed_at IS NULL AND revoked_at IS NULL", (now,)
        )
        conn.execute(
            "INSERT INTO installation_claims (id,token_hash,created_at,expires_at) VALUES (?,?,?,?)",
            (claim_id, sha256_text(token), now, expires_at),
        )
        audit(conn, "bootstrap.issue", claim_id, kind="operator")
    return {"token": token, "expires_at": expires_at, **metadata}


def _valid(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM installation_claims WHERE token_hash=? AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at>?",
        (sha256_text(token), now_iso()),
    ).fetchone()
    if row is None or conn.execute("SELECT 1 FROM users WHERE status='active' AND is_platform_admin=1").fetchone():
        raise ValidationFailed(INVALID)
    return row


def inspect(ctx: AppContext, token: str) -> dict[str, str]:
    _settings(ctx)
    with ctx.db.read() as conn:
        row = _valid(conn, token)
        team = conn.execute("SELECT name FROM teams WHERE id=?", (DEFAULT_TEAM_ID,)).fetchone()
        assert team is not None
        return {"expires_at": row["expires_at"], "team_id": DEFAULT_TEAM_ID, "team_name": team["name"]}


def complete(
    ctx: AppContext,
    token: str,
    name: str,
    password: str,
    display_name: str,
    team_name: str,
    *,
    browser_user_id: str | None = None,
) -> tuple[str, str, str]:
    _settings(ctx)
    if browser_user_id is not None:
        raise Conflict("Sign out of the current account before initializing the administrator.")
    name, team_name = username(name), team_name.strip()
    if not team_name or len(team_name) > 200:
        raise ValidationFailed("Give the team a name of at most 200 characters.")
    with ctx.db.read() as conn:
        _valid(conn, token)
    encoded = hash_password(password)
    user_id, now = new_id(), now_iso()
    with ctx.db.write() as conn:
        claim = _valid(conn, token)
        if conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
            raise Conflict("That username already exists. Recover the existing account instead.")
        conn.execute(
            "INSERT INTO users (id,username,display_name,status,is_platform_admin,created_at,updated_at) VALUES (?,?,?,'active',1,?,?)",
            (user_id, name, display_name.strip() or name, now, now),
        )
        conn.execute("INSERT INTO local_credentials VALUES (?,?,?)", (user_id, encoded, now))
        conn.execute("INSERT INTO team_memberships VALUES (?,?,'owner',?,?)", (DEFAULT_TEAM_ID, user_id, now, now))
        conn.execute("UPDATE teams SET name=? WHERE id=?", (team_name, DEFAULT_TEAM_ID))
        conn.execute("UPDATE auth_state SET local_enabled=1,activated_at=? WHERE id=1", (now,))
        conn.execute("UPDATE installation_claims SET consumed_at=? WHERE id=?", (now, claim["id"]))
        session_token, session_id = insert_session(conn, ctx, user_id, 0)
        audit(conn, "bootstrap.complete", claim["id"], actor=user_id)
        return session_token, session_id, DEFAULT_TEAM_ID
