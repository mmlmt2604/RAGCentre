"""A populated schema 7 upgrades additively without adopting pending identities."""

from __future__ import annotations

from pathlib import Path

from ragcentre.accounts.passwords import hash_password
from ragcentre.accounts.users import DEFAULT_TEAM_ID, complete_setup
from ragcentre.context import open_context
from ragcentre.db.connection import Database
from ragcentre.db.migrate import available_migrations, latest_version, migrate
from ragcentre.hashing import sha256_text
from ragcentre.settings import Settings
from ragcentre.timeutil import after, now_iso

PASSWORD = "a long existing account password"


def test_schema7_accounts_sessions_setup_memberships_and_source_state_survive(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, workers=0, auth_mode="local", browser_origin="https://testserver")
    db = Database(settings.db_path)
    now, setup, session = (
        now_iso(),
        "legacy-setup-token-with-enough-entropy",
        "legacy-session-token-with-enough-entropy",
    )
    try:
        with db.connection() as conn:
            for migration in available_migrations()[:7]:
                conn.executescript(migration.sql)
            conn.execute("PRAGMA user_version=7")
            for name, status, admin in (
                ("administrator", "active", 1),
                ("pending", "pending", 0),
                ("disabled", "disabled", 0),
            ):
                conn.execute(
                    "INSERT INTO users (id,username,display_name,status,is_platform_admin,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (name, name, name.title(), status, admin, now, now),
                )
            conn.execute(
                "INSERT INTO local_credentials VALUES (?,?,?)", ("administrator", hash_password(PASSWORD), now)
            )
            conn.execute(
                "INSERT INTO team_memberships VALUES (?,?,'owner',?,?)", (DEFAULT_TEAM_ID, "administrator", now, now)
            )
            conn.execute(
                "INSERT INTO user_sessions VALUES (?,?,?,?,?,?,?,?)",
                ("session", sha256_text(session), "administrator", 0, "csrf", now, now, after(86400)),
            )
            conn.execute(
                "INSERT INTO account_setup_tokens VALUES (?,?,?,?,NULL)",
                ("setup", "pending", sha256_text(setup), after(86400)),
            )
            conn.execute("UPDATE auth_state SET local_enabled=1,activated_at=?", (now,))
            conn.execute(
                "INSERT INTO corpora (id,name,metadata_schema,created_at) VALUES ('corpus','Existing corpus','{}',?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO providers (id,name,kind,type,config,fingerprint,created_at,updated_at,team_id) VALUES ('connector','Existing connector','connector','s3','{}','stable-fingerprint',?,?,?)",
                (now, now, DEFAULT_TEAM_ID),
            )
            conn.execute(
                "INSERT INTO sync_sources (id,corpus_id,provider_id,name,scope,schedule_seconds,cursor,status,created_at,updated_at) VALUES ('source','corpus','connector','Existing source','{}',3600,'stable-cursor','idle',?,?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO source_items (source_id,remote_id,remote_version,last_seen_at) VALUES ('source','remote','stable-version',?)",
                (now,),
            )
        tables = (
            "users",
            "local_credentials",
            "team_memberships",
            "user_sessions",
            "account_setup_tokens",
            "auth_state",
            "providers",
            "corpora",
            "sync_sources",
            "source_items",
        )
        before = {table: [dict(row) for row in db.all(f"SELECT * FROM {table}")] for table in tables}
        assert migrate(db) == list(range(8, latest_version() + 1))
        assert {table: [dict(row) for row in db.all(f"SELECT * FROM {table}")] for table in tables} == before
        assert db.scalar("SELECT count(*) FROM team_invitations") == 0
        assert db.scalar("SELECT count(*) FROM password_recovery_tokens") == 0
        assert db.scalar("SELECT count(*) FROM installation_claims") == 0
    finally:
        db.close()
    ctx = open_context(settings)
    try:
        assert ctx.sessions.resolve(session) is not None
        complete_setup(ctx, setup, PASSWORD)
        assert ctx.db.scalar("SELECT status FROM users WHERE id='pending'") == "active"
        assert ctx.db.scalar("SELECT count(*) FROM team_memberships WHERE user_id='pending'") == 0
        assert ctx.db.scalar("SELECT cursor FROM sync_sources WHERE id='source'") == "stable-cursor"
    finally:
        ctx.close()
