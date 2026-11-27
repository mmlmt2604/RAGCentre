"""Migration-0005 atomicity, coordinated startup, and restored account integrity."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ragcentre.context import open_context
from ragcentre.db import migrate as migration
from ragcentre.db.connection import Database
from ragcentre.settings import Settings


def version_four(path: Path) -> Database:
    db = Database(path)
    with db.write() as conn:
        for item in migration.available_migrations()[:4]:
            for statement in migration.split_statements(item.sql):
                conn.execute(statement)
        conn.execute("PRAGMA user_version = 4")
    return db


def test_upgrade_preserves_existing_rows_and_is_idempotent(tmp_path: Path) -> None:
    db = version_four(tmp_path / "upgrade.db")
    try:
        db.execute(
            "INSERT INTO corpora (id, name, metadata_schema, created_at) VALUES (?, ?, ?, ?)",
            ("pre-auth-corpus", "Existing corpus", '{"fields":{"language":{"type":"str"}}}', "2026-11-26T00:00:00Z"),
        )
        original_corpus = tuple(db.one("SELECT * FROM corpora WHERE id = 'pre-auth-corpus'"))
        before = {
            row["name"]: db.scalar(f"SELECT COUNT(*) FROM {row['name']}")
            for row in db.all(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%fts%' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert migration.migrate(db) == [5]
        assert migration.migrate(db) == []
        assert migration.verify(db) == 5
        for table, count in before.items():
            assert db.scalar(f"SELECT COUNT(*) FROM {table}") == count
        assert db.scalar("SELECT COUNT(*) FROM users") == 0
        assert tuple(db.one("SELECT * FROM corpora WHERE id = 'pre-auth-corpus'")) == original_corpus
        assert db.scalar("PRAGMA integrity_check") == "ok"
        assert db.all("PRAGMA foreign_key_check") == []
    finally:
        db.close()


_STATEMENTS = migration.split_statements(migration.available_migrations()[-1].sql)


@pytest.mark.parametrize("position", range(len(_STATEMENTS) + 1))
def test_account_migration_rolls_back_at_each_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: int
) -> None:
    db = version_four(tmp_path / "atomic.db")
    items = migration.available_migrations()
    statements = [*_STATEMENTS[:position], "THIS IS INTENTIONALLY INVALID SQL;", *_STATEMENTS[position:]]
    broken = migration.Migration(5, items[-1].name, "\n".join(statements))
    monkeypatch.setattr(migration, "available_migrations", lambda: [*items[:4], broken])
    try:
        with pytest.raises(sqlite3.OperationalError):
            migration.migrate(db)
        assert db.scalar("PRAGMA user_version") == 4
        assert (
            db.all(
                "SELECT name FROM sqlite_master WHERE name IN ('users', 'local_credentials', 'user_sessions', 'api_keys')"
            )
            == []
        )
        assert db.scalar("PRAGMA integrity_check") == "ok"
        monkeypatch.setattr(migration, "available_migrations", lambda: items)
        assert migration.migrate(db) == [5]
    finally:
        db.close()


def test_concurrent_connections_apply_accounts_once(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    initial = version_four(path)
    initial.close()
    barrier = threading.Barrier(2)

    def start() -> list[int]:
        db = Database(path)
        try:
            barrier.wait(timeout=5)
            return migration.migrate(db)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: start(), range(2)))
    assert sorted(results, key=len) == [[], [5]]
    db = Database(path)
    try:
        assert migration.verify(db) == 5
        assert db.scalar("PRAGMA integrity_check") == "ok"
    finally:
        db.close()


def test_newer_database_refused_without_schema_changes(tmp_path: Path) -> None:
    db = version_four(tmp_path / "future.db")
    try:
        db.execute("PRAGMA user_version = 99")
        before = [tuple(row) for row in db.all("SELECT name, sql FROM sqlite_master ORDER BY name")]
        with pytest.raises(migration.StartupError, match="schema version 99"):
            migration.migrate(db)
        assert db.scalar("PRAGMA user_version") == 99
        assert [tuple(row) for row in db.all("SELECT name, sql FROM sqlite_master ORDER BY name")] == before
    finally:
        db.close()


def test_backup_restore_preserves_account_keys_and_sessions(tmp_path: Path) -> None:
    source = open_context(Settings(data_dir=tmp_path / "source", workers=0, discovery_urls="off"))
    backup = tmp_path / "backup.db"
    password = "release-backup-password"
    try:
        user = source.accounts.create_account("restore-user", "Restore User", password)
        token, csrf = source.accounts.create_session(user["id"], expected_auth_version=user["auth_version"])
        key = source.accounts.create_key("restore-key", "read")
        with source.db.connection() as conn, sqlite3.connect(backup) as target:
            conn.backup(target)
    finally:
        source.close()
    destination = tmp_path / "restored"
    destination.mkdir()
    # Use SQLite's backup API rather than copying a live main DB without its WAL.
    with sqlite3.connect(backup) as saved, sqlite3.connect(destination / "ragcentre.db") as target:
        saved.backup(target)
    restored = open_context(Settings(data_dir=destination, workers=0, discovery_urls="off"))
    try:
        assert restored.accounts.verify_login("restore-user", password) is not None
        assert restored.accounts.session_for(token)["csrf_secret"] == csrf
        assert restored.accounts.key_for(key["key"])["scope"] == "read"
        assert restored.db.scalar("PRAGMA integrity_check") == "ok"
        assert restored.db.all("PRAGMA foreign_key_check") == []
        assert restored.db.scalar("SELECT token_hash FROM user_sessions") != token
        assert restored.db.scalar("SELECT token_hash FROM api_keys") != key["key"]
        assert restored.db.scalar("SELECT password_hash FROM local_credentials").startswith("$argon2id$")
    finally:
        restored.close()
