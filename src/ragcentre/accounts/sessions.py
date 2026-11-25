"""Opaque sessions and live identity/team resolution."""

from __future__ import annotations

import secrets
import threading
from time import monotonic
from typing import TYPE_CHECKING, Any

from ragcentre.accounts.passwords import LoginLimiter, hash_password, needs_rehash, verify_password
from ragcentre.accounts.users import audit, user_out
from ragcentre.errors import Unauthorized, ValidationFailed
from ragcentre.hashing import new_id, sha256_text
from ragcentre.timeutil import after, now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext


class Sessions:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.limiter = LoginLimiter()
        self._cleanup_lock = threading.Lock()
        self._last_cleanup = 0.0

    def login(self, name: str, password: str, peer: str) -> tuple[str, str]:
        name = name.strip().lower()
        self.limiter.check(f"user:{name}", f"peer:{peer}")
        row = self.ctx.db.one(
            "SELECT u.id, u.status, u.auth_version, c.password_hash FROM users u LEFT JOIN local_credentials c ON c.user_id=u.id WHERE u.username=?",
            (name,),
        )
        encoded = row["password_hash"] if row is not None and row["status"] == "active" else None
        if not verify_password(encoded, password) or row is None or encoded is None:
            raise Unauthorized("Invalid username or password.")
        upgraded = hash_password(password) if needs_rehash(encoded) else None
        token, session_id, now = secrets.token_urlsafe(32), new_id(), now_iso()
        with self.ctx.db.write() as conn:
            current = conn.execute("SELECT status, auth_version FROM users WHERE id=?", (row["id"],)).fetchone()
            if current is None or current["status"] != "active" or current["auth_version"] != row["auth_version"]:
                raise Unauthorized("Invalid username or password.")
            if upgraded:
                conn.execute("UPDATE local_credentials SET password_hash=? WHERE user_id=?", (upgraded, row["id"]))
            conn.execute(
                "INSERT INTO user_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    sha256_text(token),
                    row["id"],
                    row["auth_version"],
                    secrets.token_urlsafe(32),
                    now,
                    now,
                    after(self.ctx.settings.session_lifetime_seconds),
                ),
            )
            audit(conn, "session.login", session_id, actor=row["id"])
        self.prune()
        return token, session_id

    def resolve(self, token: str) -> dict[str, Any] | None:
        if len(token) > 128:
            return None
        row = self.ctx.db.one(
            "SELECT s.*, u.status, u.is_platform_admin, u.auth_version AS current_version FROM user_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (sha256_text(token),),
        )
        now = now_iso()
        if (
            row is None
            or row["status"] != "active"
            or row["auth_version"] != row["current_version"]
            or row["expires_at"] <= now
            or row["last_seen_at"] <= after(-self.ctx.settings.session_idle_seconds)
        ):
            return None
        if row["last_seen_at"] < after(-60):
            self.ctx.db.execute(
                "UPDATE user_sessions SET last_seen_at=? WHERE id=? AND last_seen_at < ?", (now, row["id"], after(-60))
            )
        self.prune()
        return dict(row)

    def current(self, user_id: str, csrf_token: str) -> dict[str, Any]:
        memberships = self.ctx.db.all(
            "SELECT t.id AS team_id, t.name AS team_name, t.slug AS team_slug, m.role FROM team_memberships m JOIN teams t ON t.id=m.team_id WHERE m.user_id=? ORDER BY t.name, t.id",
            (user_id,),
        )
        return {
            "user": user_out(self.ctx, user_id),
            "memberships": [dict(m) for m in memberships],
            "csrf_token": csrf_token,
        }

    def logout(self, session_id: str, user_id: str) -> None:
        with self.ctx.db.write() as conn:
            conn.execute("DELETE FROM user_sessions WHERE id=? AND user_id=?", (session_id, user_id))
            audit(conn, "session.logout", session_id, actor=user_id)

    def change_password(self, user_id: str, current_password: str, password: str, peer: str) -> None:
        self.limiter.check(f"password:{user_id}", f"peer:{peer}")
        row = self.ctx.db.one(
            "SELECT u.auth_version, c.password_hash FROM users u JOIN local_credentials c ON c.user_id=u.id WHERE u.id=? AND u.status='active'",
            (user_id,),
        )
        if row is None or not verify_password(row["password_hash"], current_password):
            raise Unauthorized("The current password was not accepted.")
        encoded = hash_password(password)
        with self.ctx.db.write() as conn:
            changed = conn.execute(
                "UPDATE users SET auth_version=auth_version+1, updated_at=? WHERE id=? AND auth_version=? AND status='active'",
                (now_iso(), user_id, row["auth_version"]),
            ).rowcount
            if not changed:
                raise Unauthorized("Your account changed. Sign in again.")
            conn.execute(
                "UPDATE local_credentials SET password_hash=?, password_changed_at=? WHERE user_id=?",
                (encoded, now_iso(), user_id),
            )
            conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
            audit(conn, "user.password-change", user_id, actor=user_id)

    def prune(self) -> None:
        if monotonic() - self._last_cleanup < 300 or not self._cleanup_lock.acquire(blocking=False):
            return
        try:
            self._last_cleanup = monotonic()
            with self.ctx.db.write() as conn:
                conn.execute(
                    "DELETE FROM user_sessions WHERE id IN (SELECT id FROM user_sessions WHERE expires_at<=? OR last_seen_at<=? LIMIT 100)",
                    (now_iso(), after(-self.ctx.settings.session_idle_seconds)),
                )
                conn.execute(
                    "DELETE FROM account_setup_tokens WHERE id IN (SELECT id FROM account_setup_tokens WHERE expires_at<=? OR used_at IS NOT NULL LIMIT 100)",
                    (now_iso(),),
                )
                conn.execute(
                    "DELETE FROM audit_events WHERE id IN (SELECT id FROM audit_events WHERE created_at<? LIMIT 100)",
                    (after(-86400 * self.ctx.settings.audit_retention_days),),
                )
        finally:
            self._cleanup_lock.release()


def local_enabled(ctx: AppContext) -> bool:
    return ctx.settings.auth_mode == "local" or bool(ctx.db.scalar("SELECT local_enabled FROM auth_state WHERE id=1"))


def validate_mode(ctx: AppContext) -> None:
    settings = ctx.settings
    if settings.auth_mode not in {None, "legacy", "local"}:
        raise ValidationFailed("RAGCENTRE_AUTH_MODE must be legacy or local.")
    if settings.auth_mode == "legacy" and local_enabled(ctx):
        raise ValidationFailed(
            "Local accounts are activated for this database; legacy mode cannot disable authentication."
        )
    if min(settings.session_idle_seconds, settings.session_lifetime_seconds, settings.audit_retention_days) <= 0:
        raise ValidationFailed("Session lifetimes and audit retention must be positive.")
