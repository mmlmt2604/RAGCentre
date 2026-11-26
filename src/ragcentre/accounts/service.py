"""The single-account service: users, cookie sessions and named API keys.

All database access goes through ``ctx.db``. Session and key secrets are generated here and stored
only as SHA-256 hashes; the plaintext is returned exactly once to the caller (login/setup for a
session token, key creation for a key secret) and never written back.

Passwords are pure functions in :mod:`ragcentre.accounts.passwords`; this module keeps the
transactional, database-backed logic.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

from ragcentre.accounts.passwords import (
    hash_password,
    normalize_username,
    validate_password,
    validate_username,
    verify_password,
)
from ragcentre.errors import Conflict, NotFound, ValidationFailed
from ragcentre.hashing import new_id, sha256_text
from ragcentre.timeutil import after, now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext

SESSION_COOKIE = "ragcentre_session"
SESSION_LIFETIME_SECONDS = 30 * 24 * 3600  # 30 days, absolute; no idle timeout in single-user mode.
KEY_LAST_USED_THROTTLE_SECONDS = 60

_dummy_hash: str | None = None


def _dummy_verify(password: str) -> None:
    """Burn the same Argon2 time as a real verification for unknown users, so login timing does not
    reveal whether a username exists."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("ragcentre-login-timing-equalizer")
    verify_password(_dummy_hash, password)


class Accounts:
    """Account, session and API-key operations, backed by the app context's database."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ---- users ---------------------------------------------------------------------------------

    def has_accounts(self) -> bool:
        return self.ctx.db.scalar("SELECT 1 FROM users LIMIT 1") is not None

    def account(self) -> dict[str, Any] | None:
        row = self.ctx.db.one("SELECT * FROM users ORDER BY rowid LIMIT 1")
        return dict(row) if row is not None else None

    def _user_by_username(self, username: str) -> dict[str, Any] | None:
        row = self.ctx.db.one("SELECT * FROM users WHERE username = ?", (username,))
        return dict(row) if row is not None else None

    def create_account(self, username: str, display_name: str, password: str) -> dict[str, Any]:
        """Create the single account and its credential, transactionally. Raises ``Conflict`` if an
        account already exists (there is no second account in this slice)."""
        username = validate_username(username)
        validate_password(password)
        password_hash = hash_password(password)
        user_id = new_id()
        now = now_iso()
        with self.ctx.db.write() as conn:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
                raise Conflict("An account already exists.")
            conn.execute(
                "INSERT INTO users (id, username, display_name, status, is_platform_admin, auth_version, "
                "created_at, updated_at) VALUES (?, ?, ?, 'active', 1, 0, ?, ?)",
                (user_id, username, display_name.strip(), now, now),
            )
            conn.execute(
                "INSERT INTO local_credentials (user_id, password_hash, password_changed_at) VALUES (?, ?, ?)",
                (user_id, password_hash, now),
            )
        return {
            "id": user_id,
            "username": username,
            "display_name": display_name.strip(),
            "is_platform_admin": True,
        }

    def verify_login(self, username: str, password: str) -> dict[str, Any] | None:
        """Return the account when credentials are valid, else ``None`` (callers keep the error generic)."""
        user = self._user_by_username(normalize_username(username))
        if user is None:
            _dummy_verify(password)
            return None
        cred = self.ctx.db.one("SELECT password_hash FROM local_credentials WHERE user_id = ?", (user["id"],))
        if cred is None:
            _dummy_verify(password)
            return None
        if not verify_password(cred["password_hash"], password):
            return None
        if user["status"] != "active":
            return None
        return user

    def change_password(self, user_id: str, current_password: str, new_password: str) -> None:
        cred = self.ctx.db.one("SELECT password_hash FROM local_credentials WHERE user_id = ?", (user_id,))
        if cred is None:
            raise NotFound("Account credential not found.")
        if not verify_password(cred["password_hash"], current_password):
            raise ValidationFailed("The current password is wrong.")
        self.reset_password(user_id, new_password)

    def reset_password(self, user_id: str, new_password: str) -> None:
        """Set a new password and revoke every session (operator recovery, password change)."""
        validate_password(new_password)
        new_hash = hash_password(new_password)
        now = now_iso()
        with self.ctx.db.write() as conn:
            conn.execute(
                "UPDATE local_credentials SET password_hash = ?, password_changed_at = ? WHERE user_id = ?",
                (new_hash, now, user_id),
            )
            conn.execute(
                "UPDATE users SET auth_version = auth_version + 1, updated_at = ? WHERE id = ?", (now, user_id)
            )
            conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))

    # ---- sessions ------------------------------------------------------------------------------

    def create_session(self, user_id: str) -> tuple[str, str]:
        """Create a session and return ``(session_token, csrf_secret)``. The token is stored hashed."""
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = now_iso()
        with self.ctx.db.write() as conn:
            conn.execute(
                "INSERT INTO user_sessions (id, user_id, token_hash, auth_version, csrf_secret, created_at, "
                "last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    user_id,
                    sha256_text(token),
                    0,
                    csrf,
                    now,
                    now,
                    after(SESSION_LIFETIME_SECONDS),
                ),
            )
        return token, csrf

    def session_for(self, token: str) -> dict[str, Any] | None:
        """Resolve a valid session from its raw token. ``None`` when unknown, expired, or the user's
        ``auth_version`` no longer matches (password change/disable invalidates sessions)."""
        row = self.ctx.db.one(
            "SELECT s.id, s.user_id, s.csrf_secret, s.auth_version, s.expires_at, u.status, u.auth_version AS "
            "user_auth_version FROM user_sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (sha256_text(token),),
        )
        if row is None:
            return None
        if row["expires_at"] < now_iso():
            return None
        if row["status"] != "active" or row["auth_version"] != row["user_auth_version"]:
            return None
        return {"user_id": row["user_id"], "csrf_secret": row["csrf_secret"], "session_id": row["id"]}

    def revoke_session(self, token: str) -> None:
        self.ctx.db.execute("DELETE FROM user_sessions WHERE token_hash = ?", (sha256_text(token),))

    # ---- API keys ------------------------------------------------------------------------------

    def create_key(self, name: str, scope: str) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValidationFailed("Give the key a name.")
        if scope not in ("read", "full"):
            raise ValidationFailed("Key scope must be 'read' or 'full'.")
        secret = secrets.token_urlsafe(32)
        key_id = new_id()
        now = now_iso()
        with self.ctx.db.write() as conn:
            conn.execute(
                "INSERT INTO api_keys (id, name, scope, token_hash, team_id, created_by_user_id, created_at) "
                "VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                (key_id, name, scope, sha256_text(secret), now),
            )
        return {"id": key_id, "name": name, "scope": scope, "key": secret, "created_at": now, "last_used_at": None}

    def list_keys(self) -> list[dict[str, Any]]:
        rows = self.ctx.db.all("SELECT id, name, scope, created_at, last_used_at FROM api_keys ORDER BY rowid")
        return [dict(r) for r in rows]

    def revoke_key(self, key_id: str) -> bool:
        with self.ctx.db.write() as conn:
            cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
            return cur.rowcount > 0

    def key_for(self, secret: str) -> dict[str, Any] | None:
        row = self.ctx.db.one("SELECT id, scope FROM api_keys WHERE token_hash = ?", (sha256_text(secret),))
        if row is None:
            return None
        self._touch_key(row["id"])
        return {"id": row["id"], "scope": row["scope"]}

    def _touch_key(self, key_id: str) -> None:
        """Record use at most once a minute, so a busy key doesn't write on every request."""
        self.ctx.db.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)",
            (now_iso(), key_id, after(-KEY_LAST_USED_THROTTLE_SECONDS)),
        )
