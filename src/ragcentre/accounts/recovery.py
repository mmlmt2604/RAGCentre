"""Administrator-authorized recovery for active, non-administrator accounts."""

from __future__ import annotations

import secrets
import sqlite3
from typing import TYPE_CHECKING, Any

from ragcentre.accounts.credentials import authority, invalidate_issuer, invalidate_target, link_metadata
from ragcentre.accounts.passwords import hash_password, verify_password
from ragcentre.accounts.users import audit
from ragcentre.errors import Conflict, Forbidden, Unauthorized, ValidationFailed
from ragcentre.hashing import new_id, sha256_text
from ragcentre.timeutil import after, now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext

INVALID = "The recovery link is invalid or no longer usable."


def issue(
    ctx: AppContext, user_id: str, current_password: str, *, actor: str, session_id: str, peer: str
) -> dict[str, Any]:
    ctx.sessions.limiter.check(f"recovery-issue:{actor}", f"peer:{peer}")
    row = ctx.db.one(
        "SELECT u.auth_version,c.password_hash FROM users u JOIN local_credentials c ON c.user_id=u.id WHERE u.id=? AND u.status='active' AND u.is_platform_admin=1",
        (actor,),
    )
    if not verify_password(row["password_hash"] if row else None, current_password) or row is None:
        raise Unauthorized("The current administrator password was not accepted.")
    token, expires_at, now, recovery_id = secrets.token_urlsafe(32), after(1800), now_iso(), new_id()
    metadata = link_metadata(ctx.settings, "recover", token)
    with ctx.db.write() as conn:
        issuer = authority(conn, actor)
        if (
            issuer["auth_version"] != row["auth_version"]
            or conn.execute(
                "SELECT 1 FROM user_sessions WHERE id=? AND user_id=? AND auth_version=? AND expires_at>? AND last_seen_at>?",
                (session_id, actor, row["auth_version"], now, after(-ctx.settings.session_idle_seconds)),
            ).fetchone()
            is None
        ):
            raise Unauthorized("Your account changed. Sign in again.")
        target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if target is None or target["status"] != "active" or target["is_platform_admin"]:
            raise Conflict(
                "Recovery links require an active non-administrator account. Use operator recovery for administrators."
            )
        conn.execute(
            "UPDATE password_recovery_tokens SET revoked_at=? WHERE user_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
            (now, user_id),
        )
        conn.execute(
            "INSERT INTO password_recovery_tokens (id,user_id,issuer_user_id,issuer_auth_version,target_auth_version,token_hash,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                recovery_id,
                user_id,
                actor,
                issuer["auth_version"],
                target["auth_version"],
                sha256_text(token),
                now,
                expires_at,
            ),
        )
        audit(conn, "recovery.issue", recovery_id, actor=actor)
    return {"token": token, "expires_at": expires_at, **metadata}


def _valid(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT t.*,u.username,u.display_name FROM password_recovery_tokens t JOIN users u ON u.id=t.user_id "
        "WHERE t.token_hash=? AND t.consumed_at IS NULL AND t.revoked_at IS NULL AND t.expires_at>? "
        "AND u.status='active' AND u.is_platform_admin=0 AND u.auth_version=t.target_auth_version",
        (sha256_text(token), now_iso()),
    ).fetchone()
    if row is None:
        raise ValidationFailed(INVALID)
    try:
        issuer = authority(conn, row["issuer_user_id"])
    except Forbidden:
        raise ValidationFailed(INVALID) from None
    if issuer["auth_version"] != row["issuer_auth_version"]:
        raise ValidationFailed(INVALID)
    return row


def inspect(ctx: AppContext, token: str) -> dict[str, str]:
    with ctx.db.read() as conn:
        row = _valid(conn, token)
        return {key: row[key] for key in ("username", "display_name", "expires_at")}


def complete(ctx: AppContext, token: str, password: str) -> None:
    with ctx.db.read() as conn:
        _valid(conn, token)
    encoded = hash_password(password)
    with ctx.db.write() as conn:
        row = _valid(conn, token)
        now = now_iso()
        conn.execute("UPDATE password_recovery_tokens SET consumed_at=? WHERE id=?", (now, row["id"]))
        conn.execute(
            "UPDATE local_credentials SET password_hash=?,password_changed_at=? WHERE user_id=?",
            (encoded, now, row["user_id"]),
        )
        conn.execute("UPDATE users SET auth_version=auth_version+1,updated_at=? WHERE id=?", (now, row["user_id"]))
        conn.execute("DELETE FROM user_sessions WHERE user_id=?", (row["user_id"],))
        conn.execute("DELETE FROM account_setup_tokens WHERE user_id=?", (row["user_id"],))
        invalidate_target(conn, row["user_id"])
        invalidate_issuer(conn, row["user_id"])
        audit(conn, "recovery.complete", row["id"], actor=row["user_id"])
