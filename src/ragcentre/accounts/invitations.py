"""Team-scoped invitations, including atomic reservation and account activation."""

from __future__ import annotations

import secrets
import sqlite3
from typing import TYPE_CHECKING, Any

from ragcentre.accounts.credentials import authority, link_metadata
from ragcentre.accounts.passwords import hash_password
from ragcentre.accounts.sessions import insert_session
from ragcentre.accounts.users import Role, audit, ensure_owner, username
from ragcentre.errors import Conflict, Forbidden, NotFound, ValidationFailed
from ragcentre.hashing import new_id, sha256_text
from ragcentre.timeutil import after, now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext

INVALID = "The invitation link is invalid or no longer usable."


def _isolated(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    return bool(
        row["kind"] == "new_account"
        and row["created_user_id"] == row["user_id"]
        and row["status"] == "pending"
        and not row["is_platform_admin"]
        and row["auth_version"] == row["target_auth_version"]
        and conn.execute("SELECT 1 FROM local_credentials WHERE user_id=?", (row["user_id"],)).fetchone() is None
        and conn.execute("SELECT 1 FROM team_memberships WHERE user_id=?", (row["user_id"],)).fetchone() is None
        and conn.execute("SELECT 1 FROM account_setup_tokens WHERE user_id=?", (row["user_id"],)).fetchone() is None
        and conn.execute(
            "SELECT 1 FROM team_invitations WHERE user_id=? AND id<>? AND accepted_at IS NULL AND revoked_at IS NULL",
            (row["user_id"], row["id"]),
        ).fetchone()
        is None
    )


def _joined(conn: sqlite3.Connection, invitation_id: str, team_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT i.*, u.username, u.display_name, u.status, u.is_platform_admin, u.auth_version, t.name AS team_name "
        "FROM team_invitations i JOIN users u ON u.id=i.user_id JOIN teams t ON t.id=i.team_id WHERE i.id=? AND i.team_id=?",
        (invitation_id, team_id),
    ).fetchone()
    if row is None:
        raise NotFound("Invitation was not found.")
    return row


def _valid(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT i.id, i.team_id FROM team_invitations i WHERE token_hash=? AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at>?",
        (sha256_text(token), now_iso()),
    ).fetchone()
    if row is None:
        raise ValidationFailed(INVALID)
    row = _joined(conn, row["id"], row["team_id"])
    try:
        issuer = authority(conn, row["issuer_user_id"], row["team_id"])
    except Forbidden:
        raise ValidationFailed(INVALID) from None
    if issuer["auth_version"] != row["issuer_auth_version"] or row["auth_version"] != row["target_auth_version"]:
        raise ValidationFailed(INVALID)
    if row["status"] == "disabled" or (row["status"] == "pending" and not _isolated(conn, row)):
        raise ValidationFailed(INVALID)
    return row


def invitation_out(conn: sqlite3.Connection, invitation_id: str, team_id: str) -> dict[str, Any]:
    row = _joined(conn, invitation_id, team_id)
    state = (
        "accepted"
        if row["accepted_at"]
        else "revoked"
        if row["revoked_at"]
        else "expired"
        if row["expires_at"] <= now_iso()
        else "pending"
    )
    fields = (
        "id",
        "team_id",
        "user_id",
        "issuer_user_id",
        "username",
        "display_name",
        "role",
        "kind",
        "created_at",
        "expires_at",
        "accepted_at",
        "revoked_at",
        "generation",
    )
    return {**{key: row[key] for key in fields}, "status": state}


def _issued(ctx: AppContext, conn: sqlite3.Connection, invitation_id: str, team_id: str, token: str) -> dict[str, Any]:
    invitation = invitation_out(conn, invitation_id, team_id)
    return {
        "invitation": invitation,
        "token": token,
        "expires_at": invitation["expires_at"],
        **link_metadata(ctx.settings, "join", token),
    }


def issue(ctx: AppContext, team_id: str, name: str, display_name: str, role: Role, *, actor: str) -> dict[str, Any]:
    name = username(name)
    token, invitation_id, now = secrets.token_urlsafe(32), new_id(), now_iso()
    link_metadata(ctx.settings, "join", token)
    with ctx.db.write() as conn:
        issuer = authority(conn, actor, team_id)
        ensure_owner(conn, team_id)
        target = conn.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        created_user_id = None
        if target is None:
            created_user_id = new_id()
            conn.execute(
                "INSERT INTO users (id,username,display_name,status,created_at,updated_at) VALUES (?,?,?,'pending',?,?)",
                (created_user_id, name, display_name.strip() or name, now, now),
            )
            target = conn.execute("SELECT * FROM users WHERE id=?", (created_user_id,)).fetchone()
        else:
            if target["status"] != "active":
                raise Conflict("This account cannot be invited. Ask an administrator to resolve its account setup.")
        assert target is not None
        if conn.execute(
            "SELECT 1 FROM team_memberships WHERE team_id=? AND user_id=?", (team_id, target["id"])
        ).fetchone():
            raise Conflict("Already a member. Change their role through member management.")
        conn.execute(
            "UPDATE team_invitations SET revoked_at=? WHERE team_id=? AND user_id=? AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at<=?",
            (now, team_id, target["id"], now),
        )
        if conn.execute(
            "SELECT 1 FROM team_invitations WHERE team_id=? AND user_id=? AND accepted_at IS NULL AND revoked_at IS NULL",
            (team_id, target["id"]),
        ).fetchone():
            raise Conflict("A live invitation already exists. Reissue that invitation instead.")
        conn.execute(
            "INSERT INTO team_invitations (id,team_id,user_id,issuer_user_id,issuer_auth_version,target_auth_version,role,kind,created_user_id,token_hash,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                invitation_id,
                team_id,
                target["id"],
                actor,
                issuer["auth_version"],
                target["auth_version"],
                role,
                "new_account" if created_user_id else "existing_account",
                created_user_id,
                sha256_text(token),
                now,
                after(86400),
            ),
        )
        audit(conn, "invitation.issue", invitation_id, actor=actor, team_id=team_id)
        return _issued(ctx, conn, invitation_id, team_id, token)


def reissue(ctx: AppContext, team_id: str, invitation_id: str, *, actor: str) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    link_metadata(ctx.settings, "join", token)
    with ctx.db.write() as conn:
        issuer = authority(conn, actor, team_id)
        ensure_owner(conn, team_id)
        row = _joined(conn, invitation_id, team_id)
        if row["accepted_at"]:
            raise Conflict("An accepted invitation cannot be reissued.")
        if conn.execute(
            "SELECT 1 FROM team_memberships WHERE team_id=? AND user_id=?", (team_id, row["user_id"])
        ).fetchone():
            raise Conflict("Already a member. Change their role through member management.")
        # The current caller becomes the issuer, including an administrator taking over after demotion.
        # A different owner cannot inherit authority over another owner's reserved pending account.
        if row["status"] == "pending":
            if not _isolated(conn, row) or (not issuer["is_platform_admin"] and actor != row["issuer_user_id"]):
                raise Conflict("Ask an administrator to resolve this account's setup.")
            kind, created_user_id = "new_account", row["created_user_id"]
        elif row["status"] == "active":
            kind, created_user_id = "existing_account", None
        else:
            raise Conflict("Ask an administrator to resolve this account's setup.")
        other = conn.execute(
            "SELECT 1 FROM team_invitations WHERE team_id=? AND user_id=? AND id<>? AND accepted_at IS NULL AND revoked_at IS NULL",
            (team_id, row["user_id"], invitation_id),
        ).fetchone()
        if other:
            raise Conflict("Another live invitation exists for this account.")
        conn.execute(
            "UPDATE team_invitations SET issuer_user_id=?,issuer_auth_version=?,target_auth_version=?,kind=?,created_user_id=?,token_hash=?,generation=generation+1,expires_at=?,revoked_at=NULL WHERE id=?",
            (
                actor,
                issuer["auth_version"],
                row["auth_version"],
                kind,
                created_user_id,
                sha256_text(token),
                after(86400),
                invitation_id,
            ),
        )
        audit(conn, "invitation.reissue", invitation_id, actor=actor, team_id=team_id)
        return _issued(ctx, conn, invitation_id, team_id, token)


def revoke(ctx: AppContext, team_id: str, invitation_id: str, *, actor: str) -> None:
    with ctx.db.write() as conn:
        authority(conn, actor, team_id)
        row = _joined(conn, invitation_id, team_id)
        if not row["accepted_at"] and not row["revoked_at"]:
            conn.execute("UPDATE team_invitations SET revoked_at=? WHERE id=?", (now_iso(), invitation_id))
            audit(conn, "invitation.revoke", invitation_id, actor=actor, team_id=team_id)


def inspect(ctx: AppContext, token: str) -> dict[str, Any]:
    with ctx.db.read() as conn:
        row = _valid(conn, token)
        return {
            **{key: row[key] for key in ("username", "display_name", "team_id", "team_name", "role", "expires_at")},
            "mode": "activate" if row["status"] == "pending" else "sign_in",
        }


def activate(
    ctx: AppContext, token: str, password: str, display_name: str | None = None, *, browser_user_id: str | None = None
) -> tuple[str, str, str]:
    with ctx.db.read() as conn:
        row = _valid(conn, token)
        if not _isolated(conn, row):
            raise ValidationFailed(INVALID)
        if browser_user_id is not None and browser_user_id != row["user_id"]:
            raise Conflict("Sign out of the current account before activating this invitation.")
    encoded = hash_password(password)
    with ctx.db.write() as conn:
        row = _valid(conn, token)
        if not _isolated(conn, row):
            raise ValidationFailed(INVALID)
        now = now_iso()
        conn.execute("INSERT INTO local_credentials VALUES (?,?,?)", (row["user_id"], encoded, now))
        display = row["display_name"] if display_name is None else display_name.strip() or row["username"]
        conn.execute(
            "UPDATE users SET status='active',display_name=?,auth_version=auth_version+1,updated_at=? WHERE id=?",
            (display, now, row["user_id"]),
        )
        conn.execute(
            "INSERT INTO team_memberships VALUES (?,?,?,?,?)", (row["team_id"], row["user_id"], row["role"], now, now)
        )
        conn.execute("UPDATE team_invitations SET accepted_at=? WHERE id=?", (now, row["id"]))
        session_token, session_id = insert_session(conn, ctx, row["user_id"], row["auth_version"] + 1)
        audit(conn, "invitation.accept", row["id"], actor=row["user_id"], team_id=row["team_id"])
        return session_token, session_id, row["team_id"]


def accept(ctx: AppContext, token: str, *, actor: str, session_id: str) -> tuple[str, bool]:
    with ctx.db.write() as conn:
        row = _valid(conn, token)
        if row["user_id"] != actor:
            raise Forbidden("Sign in as the account named in this invitation.")
        if (
            row["status"] != "active"
            or conn.execute(
                "SELECT 1 FROM user_sessions WHERE id=? AND user_id=? AND auth_version=? AND expires_at>? AND last_seen_at>?",
                (session_id, actor, row["auth_version"], now_iso(), after(-ctx.settings.session_idle_seconds)),
            ).fetchone()
            is None
        ):
            raise Forbidden("This invitation requires a current session for the intended account.")
        existing = (
            conn.execute(
                "SELECT 1 FROM team_memberships WHERE team_id=? AND user_id=?", (row["team_id"], actor)
            ).fetchone()
            is not None
        )
        now = now_iso()
        if not existing:
            conn.execute(
                "INSERT INTO team_memberships VALUES (?,?,?,?,?)", (row["team_id"], actor, row["role"], now, now)
            )
        conn.execute("UPDATE team_invitations SET accepted_at=? WHERE id=?", (now, row["id"]))
        audit(
            conn,
            "invitation.accept-existing-member" if existing else "invitation.accept",
            row["id"],
            actor=actor,
            team_id=row["team_id"],
        )
        return row["team_id"], existing
