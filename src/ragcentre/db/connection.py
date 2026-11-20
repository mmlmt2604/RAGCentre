"""Per-thread SQLite connections and a short write-transaction helper (section 11).

Connections use ``isolation_level=None`` so transactions are explicit. ``write()`` issues
``BEGIN IMMEDIATE`` and commits or rolls back. Never hold a write transaction across an HTTP
call or a vector store call.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)

Setup = Callable[[sqlite3.Connection], None]


class Database:
    def __init__(self, path: Path | str, *, setup: Setup | None = None) -> None:
        self.path = str(path)
        self._setup = setup
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._closed = False

    def connect(self) -> sqlite3.Connection:
        """Open a new connection with the standard settings.

        A fresh database is created and its pragmas applied by the first connection to touch it;
        other connections opening at the same moment can see a transient ``database is locked``
        before any transaction begins. Retry the idempotent setup a few times with backoff, then
        let the real error surface.
        """
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        last: BaseException | None = None
        for attempt in range(5):
            if attempt:
                time.sleep(0.02 * attempt)  # 20, 40, 60, 80 ms
            try:
                for pragma in PRAGMAS:
                    conn.execute(pragma)
                if self._setup is not None:
                    self._setup(conn)
                return conn
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    conn.close()
                    raise
                last = exc
        conn.close()
        raise last  # type: ignore[misc]

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's connection."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            if self._closed:
                raise RuntimeError(f"database {self.path} is closed")
            conn = self.connect()
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A ``BEGIN IMMEDIATE`` transaction. Nested use joins the outer transaction."""
        conn = self.conn
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A read transaction giving a consistent snapshot across several statements."""
        conn = self.conn
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("COMMIT")

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(sql, params).fetchone()
        return row

    def all(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def scalar(self, sql: str, params: Any = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            conns, self._all = self._all, []
        for conn in conns:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._local = threading.local()
