"""Migration runner. The schema version lives in ``PRAGMA user_version``.

Migrations are ``migrations/NNNN_name.sql`` files applied in order, each in one
``BEGIN IMMEDIATE`` transaction that re-checks the version, so two processes starting at once
against the same data directory apply each migration exactly once.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from importlib import resources

from ragcentre.db.connection import Database

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
  embedder_fp TEXT NOT NULL, role TEXT NOT NULL, text_sha256 TEXT NOT NULL,
  dim INTEGER NOT NULL, vector BLOB NOT NULL,
  PRIMARY KEY (embedder_fp, role, text_sha256)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS query_transforms (
  transform_fp TEXT NOT NULL, query_sha256 TEXT NOT NULL,
  output TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (transform_fp, query_sha256)
) WITHOUT ROWID;
"""


class StartupError(RuntimeError):
    """The database cannot be used by this version of RAGCentre."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def available_migrations() -> list[Migration]:
    found: list[Migration] = []
    root = resources.files("ragcentre.db") / "migrations"
    for entry in root.iterdir():
        m = _NAME.match(entry.name)
        if m:
            found.append(Migration(int(m.group(1)), entry.name, entry.read_text("utf-8")))
    found.sort(key=lambda mig: mig.version)
    for i, mig in enumerate(found, start=1):
        if mig.version != i:
            raise StartupError(f"migration numbering has a gap at {mig.name}")
    return found


def latest_version() -> int:
    migrations = available_migrations()
    return migrations[-1].version if migrations else 0


def split_statements(sql: str) -> list[str]:
    """Split a script into complete statements (handles semicolons inside triggers/strings)."""
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stripped = buf.strip()
            if stripped and not all(ln.strip().startswith("--") or not ln.strip() for ln in stripped.splitlines()):
                statements.append(stripped)
            buf = ""
    if buf.strip() and not all(ln.strip().startswith("--") or not ln.strip() for ln in buf.splitlines()):
        raise StartupError("migration ends with an incomplete statement")
    return statements


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def check_fts5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._rc_fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._rc_fts5_probe")
    except sqlite3.OperationalError as exc:
        raise StartupError(
            "This Python's SQLite library was built without FTS5, which RAGCentre needs for keyword "
            "indexes. Use a Python build whose sqlite3 includes FTS5 (for example one installed by "
            "uv, pyenv or Homebrew)."
        ) from exc


def migrate(db: Database) -> list[int]:
    """Apply pending migrations. Returns the versions applied by this call."""
    with db.connection() as conn:
        check_fts5(conn)
        migrations = available_migrations()
        latest = migrations[-1].version if migrations else 0
        if current_version(conn) > latest:
            raise StartupError(
                f"The database {db.path} has schema version {current_version(conn)}, but this RAGCentre "
                f"only knows versions up to {latest}. Upgrade RAGCentre."
            )
        applied: list[int] = []
        for mig in migrations:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if current_version(conn) >= mig.version:
                    conn.execute("COMMIT")
                    continue
                for stmt in split_statements(mig.sql):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {mig.version:d}")
                conn.execute("COMMIT")
                applied.append(mig.version)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return applied


def verify(db: Database) -> int:
    """Check the database is at the latest version; return it."""
    with db.connection() as conn:
        version = current_version(conn)
    latest = latest_version()
    if version != latest:
        raise StartupError(f"The database {db.path} is at schema version {version}; expected {latest}.")
    return version


def migrate_cache(cache: Database) -> None:
    with cache.write() as conn:
        for stmt in split_statements(CACHE_SCHEMA):
            conn.execute(stmt)
