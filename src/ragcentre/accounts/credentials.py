"""Shared, connection-aware lifecycle operations for purpose-bound account links."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING
from urllib.parse import quote

from ragcentre.accounts.http import validate_browser_settings
from ragcentre.errors import Forbidden, NotFound, ValidationFailed
from ragcentre.settings import Settings
from ragcentre.timeutil import now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext


def canonical_base(settings: Settings, *, required: bool = False) -> str | None:
    validate_browser_settings(settings)
    if not settings.browser_origin:
        if required:
            raise ValidationFailed("Set RAGCENTRE_BROWSER_ORIGIN before issuing browser links.")
        return None
    return settings.browser_origin + settings.browser_base_path


def link_metadata(settings: Settings, page: str, token: str) -> dict[str, str]:
    base = canonical_base(settings, required=True)
    path = f"{settings.browser_base_path}/{page}"
    return {"path": path, "link": f"{base}/{page}#token={quote(token, safe='')}"}


def authority(conn: sqlite3.Connection, actor: str, team_id: str | None = None) -> sqlite3.Row:
    user: sqlite3.Row | None = conn.execute("SELECT * FROM users WHERE id=? AND status='active'", (actor,)).fetchone()
    if user is None:
        raise Forbidden("This action requires an active local account.")
    if team_id is None:
        if not user["is_platform_admin"]:
            raise Forbidden("This action requires a platform administrator.")
    else:
        if conn.execute("SELECT 1 FROM teams WHERE id=?", (team_id,)).fetchone() is None:
            raise NotFound("Team was not found.")
        if (
            not user["is_platform_admin"]
            and conn.execute(
                "SELECT 1 FROM team_memberships WHERE team_id=? AND user_id=? AND role='owner'", (team_id, actor)
            ).fetchone()
            is None
        ):
            raise Forbidden("This action requires a current team owner.")
    return user


def invalidate_target(conn: sqlite3.Connection, user_id: str) -> None:
    """Password/setup/lifecycle changes revoke credentials instead of merely hiding them."""
    now = now_iso()
    conn.execute(
        "UPDATE team_invitations SET revoked_at=? WHERE user_id=? AND accepted_at IS NULL AND revoked_at IS NULL",
        (now, user_id),
    )
    conn.execute(
        "UPDATE password_recovery_tokens SET revoked_at=? WHERE user_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
        (now, user_id),
    )


def invalidate_issuer(conn: sqlite3.Connection, user_id: str, team_id: str | None = None) -> None:
    now = now_iso()
    conn.execute(
        "UPDATE team_invitations SET revoked_at=? WHERE issuer_user_id=? AND accepted_at IS NULL AND revoked_at IS NULL"
        + (" AND team_id=?" if team_id else ""),
        (now, user_id, team_id) if team_id else (now, user_id),
    )
    if team_id is None:
        conn.execute(
            "UPDATE password_recovery_tokens SET revoked_at=? WHERE issuer_user_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
            (now, user_id),
        )


def prune_credentials(ctx: AppContext) -> None:
    """Bounded retention cleanup, independent of connector and scheduler activity."""
    from ragcentre.timeutil import after

    cutoff = after(-86400 * ctx.settings.audit_retention_days)
    with ctx.db.write() as conn:
        for table in ("team_invitations", "password_recovery_tokens", "installation_claims"):
            conn.execute(
                f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE expires_at<? LIMIT 100)",
                (cutoff,),
            )
