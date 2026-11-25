"""Transactional account lifecycle and operator recovery."""

from __future__ import annotations

import re
import secrets
import sqlite3
from typing import TYPE_CHECKING, Any, Literal

from ragcentre.accounts.passwords import hash_password
from ragcentre.errors import Conflict, NotFound, ValidationFailed
from ragcentre.hashing import new_id, sha256_text
from ragcentre.timeutil import after, now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext

Role = Literal["viewer", "editor", "owner"]
DEFAULT_TEAM_ID = "00000000-0000-0000-0000-000000000001"


def username(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", value):
        raise ValidationFailed("Usernames use 3–64 lowercase letters, digits, dots, underscores or hyphens.")
    return value


def audit(
    conn: sqlite3.Connection,
    action: str,
    target: str | None,
    *,
    actor: str | None = None,
    kind: str = "user",
    team_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_events (created_at, actor_kind, actor_id, team_id, action, target_id) VALUES (?, ?, ?, ?, ?, ?)",
        (now_iso(), kind, actor, team_id, action, target),
    )


def user_out(ctx: AppContext, user_id: str) -> dict[str, Any]:
    row = ctx.db.one(
        "SELECT id, username, display_name, status, is_platform_admin, created_at, updated_at FROM users WHERE id = ?",
        (user_id,),
    )
    if row is None:
        raise NotFound("User was not found.")
    return {**dict(row), "is_platform_admin": bool(row["is_platform_admin"])}


def _setup_token(conn: sqlite3.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM account_setup_tokens WHERE user_id = ?", (user_id,))
    conn.execute(
        "INSERT INTO account_setup_tokens (id, user_id, token_hash, expires_at) VALUES (?, ?, ?, ?)",
        (new_id(), user_id, sha256_text(token), after(86400)),
    )
    return token


def create_user(ctx: AppContext, name: str, display_name: str, *, actor: str) -> dict[str, Any]:
    name = username(name)
    display_name = display_name.strip() or name
    user_id, now = new_id(), now_iso()
    try:
        with ctx.db.write() as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, status, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?)",
                (user_id, name, display_name, now, now),
            )
            token = _setup_token(conn, user_id)
            audit(conn, "user.create", user_id, actor=actor)
    except sqlite3.IntegrityError:
        raise Conflict("That username already exists.") from None
    return {"user": user_out(ctx, user_id), "setup_token": token}


def reissue_setup(ctx: AppContext, user_id: str, *, actor: str) -> dict[str, Any]:
    with ctx.db.write() as conn:
        row = conn.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise NotFound("User was not found.")
        if row["status"] != "pending":
            raise Conflict("Only pending accounts can receive a setup link.")
        token = _setup_token(conn, user_id)
        audit(conn, "user.setup-reissue", user_id, actor=actor)
    return {"user": user_out(ctx, user_id), "setup_token": token}


def complete_setup(ctx: AppContext, token: str, password: str) -> None:
    token_hash = sha256_text(token)
    row = ctx.db.one(
        "SELECT t.user_id FROM account_setup_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND t.used_at IS NULL AND t.expires_at > ? AND u.status='pending'",
        (token_hash, now_iso()),
    )
    if row is None:
        raise ValidationFailed("The setup link is invalid or expired.")
    encoded = hash_password(password)
    now = now_iso()
    with ctx.db.write() as conn:
        current = conn.execute(
            "SELECT t.id, t.user_id FROM account_setup_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND t.used_at IS NULL AND t.expires_at > ? AND u.status='pending'",
            (token_hash, now),
        ).fetchone()
        if current is None:
            raise ValidationFailed("The setup link is invalid or expired.")
        conn.execute("UPDATE account_setup_tokens SET used_at=? WHERE id=?", (now, current["id"]))
        conn.execute("INSERT INTO local_credentials VALUES (?, ?, ?)", (current["user_id"], encoded, now))
        conn.execute(
            "UPDATE users SET status='active', updated_at=?, auth_version=auth_version+1 WHERE id=?",
            (now, current["user_id"]),
        )
        audit(conn, "user.activate", current["user_id"], actor=current["user_id"])


def init_admin(ctx: AppContext, name: str, password: str) -> dict[str, Any]:
    name, encoded = username(name), hash_password(password)
    user_id, now = new_id(), now_iso()
    if ctx.settings.auth_mode == "legacy":
        raise ValidationFailed("Unset RAGCENTRE_AUTH_MODE or select local before initializing accounts.")
    with ctx.db.write() as conn:
        if conn.execute("SELECT 1 FROM users WHERE status='active' AND is_platform_admin=1").fetchone():
            raise Conflict("An active platform administrator already exists.")
        if conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
            raise Conflict("That username already exists. Recover the existing account instead.")
        conn.execute(
            "INSERT INTO users (id, username, display_name, status, is_platform_admin, created_at, updated_at) VALUES (?, ?, ?, 'active', 1, ?, ?)",
            (user_id, name, name, now, now),
        )
        conn.execute("INSERT INTO local_credentials VALUES (?, ?, ?)", (user_id, encoded, now))
        conn.execute("INSERT INTO team_memberships VALUES (?, ?, 'owner', ?, ?)", (DEFAULT_TEAM_ID, user_id, now, now))
        conn.execute("UPDATE auth_state SET local_enabled=1, activated_at=? WHERE id=1", (now,))
        audit(conn, "user.init-admin", user_id, kind="operator")
    return user_out(ctx, user_id)


def reset_password(ctx: AppContext, name: str, password: str) -> None:
    name, encoded = username(name), hash_password(password)
    with ctx.db.write() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()
        if row is None:
            raise NotFound("User was not found.")
        user_id, now = row["id"], now_iso()
        conn.execute(
            "INSERT INTO local_credentials VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET password_hash=excluded.password_hash, password_changed_at=excluded.password_changed_at",
            (user_id, encoded, now),
        )
        conn.execute(
            "UPDATE users SET status='active', auth_version=auth_version+1, updated_at=? WHERE id=?", (now, user_id)
        )
        conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM account_setup_tokens WHERE user_id=?", (user_id,))
        audit(conn, "user.password-recovery", user_id, kind="operator")


def ensure_owner(conn: sqlite3.Connection, team_id: str) -> None:
    if (
        conn.execute(
            "SELECT 1 FROM team_memberships m JOIN users u ON u.id=m.user_id WHERE m.team_id=? AND m.role='owner' AND u.status='active'",
            (team_id,),
        ).fetchone()
        is None
    ):
        raise Conflict("The team must retain at least one active owner.")


def set_membership(
    ctx: AppContext, team_id: str, name: str, role: Role | None, *, actor: str | None, operator: bool = False
) -> None:
    name = username(name)
    with ctx.db.write() as conn:
        if conn.execute("SELECT 1 FROM teams WHERE id=?", (team_id,)).fetchone() is None:
            raise NotFound("Team was not found.")
        row = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()
        if row is None:
            raise NotFound("User was not found.")
        user_id, now = row["id"], now_iso()
        previous = conn.execute(
            "SELECT role FROM team_memberships WHERE team_id=? AND user_id=?", (team_id, user_id)
        ).fetchone()
        if role is None:
            conn.execute("DELETE FROM team_memberships WHERE team_id=? AND user_id=?", (team_id, user_id))
        else:
            conn.execute(
                "INSERT INTO team_memberships VALUES (?, ?, ?, ?, ?) ON CONFLICT(team_id,user_id) DO UPDATE SET role=excluded.role,updated_at=excluded.updated_at",
                (team_id, user_id, role, now, now),
            )
        if (previous is not None and previous["role"] == "owner") or role == "owner":
            ensure_owner(conn, team_id)
        audit(
            conn,
            "membership.remove" if role is None else "membership.set",
            user_id,
            actor=actor,
            kind="operator" if operator else "user",
            team_id=team_id,
        )


def patch_user(ctx: AppContext, user_id: str, changes: dict[str, Any], *, actor: str) -> dict[str, Any]:
    with ctx.db.write() as conn:
        old = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if old is None:
            raise NotFound("User was not found.")
        status = changes.get("status", old["status"])
        if (
            status == "active"
            and conn.execute("SELECT 1 FROM local_credentials WHERE user_id=?", (user_id,)).fetchone() is None
        ):
            if old["status"] != "disabled":
                raise Conflict("Use an account setup link to activate a pending account.")
            status = "pending"
        admin = bool(changes.get("is_platform_admin", old["is_platform_admin"]))
        display = changes.get("display_name", old["display_name"]).strip()
        if not display:
            raise ValidationFailed("Give the user a display name.")
        conn.execute(
            "UPDATE users SET display_name=?, status=?, is_platform_admin=?, updated_at=? WHERE id=?",
            (display, status, int(admin), now_iso(), user_id),
        )
        if (
            old["is_platform_admin"]
            and old["status"] == "active"
            and (not admin or status != "active")
            and conn.execute("SELECT 1 FROM users WHERE status='active' AND is_platform_admin=1").fetchone() is None
        ):
            raise Conflict("Keep at least one active platform administrator.")
        if old["status"] == "active" and status != "active":
            for membership in conn.execute(
                "SELECT team_id FROM team_memberships WHERE user_id=? AND role='owner'", (user_id,)
            ).fetchall():
                ensure_owner(conn, membership["team_id"])
        if status != old["status"] or admin != bool(old["is_platform_admin"]):
            conn.execute("UPDATE users SET auth_version=auth_version+1 WHERE id=?", (user_id,))
            conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM account_setup_tokens WHERE user_id=?", (user_id,))
        audit(conn, "user.update", user_id, actor=actor)
    return user_out(ctx, user_id)
