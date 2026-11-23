"""Pooled SQLite connections and a short write-transaction helper (section 11).

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA secure_delete=ON",
)

Setup = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class WriteResult:
    rowcount: int


class Database:
    def __init__(self, path: Path | str, *, setup: Setup | None = None) -> None:
        self.path = str(path)
        self._setup = setup
        self._local = threading.local()
        self._idle: list[sqlite3.Connection] = []
        self._allocated = 0
        self._available = threading.Condition()
        self._closed = False

    def _connect(self) -> sqlite3.Connection:
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
            except BaseException:
                conn.close()
                raise
        conn.close()
        raise last  # type: ignore[misc]

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Lease a connection, sharing the lease within nested operations on this thread."""
        current: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if current is not None:
            yield current
            return
        with self._available:
            self._available.wait_for(lambda: self._closed or bool(self._idle) or self._allocated < 8)
            if self._closed:
                raise RuntimeError(f"database {self.path} is closed")
            conn = self._idle.pop() if self._idle else None
            if conn is None:
                self._allocated += 1
        if conn is None:
            try:
                conn = self._connect()
            except BaseException:
                with self._available:
                    self._allocated -= 1
                    self._available.notify()
                raise
        self._local.conn = conn
        try:
            yield conn
        finally:
            del self._local.conn
            try:
                if conn.in_transaction:
                    conn.rollback()
            finally:
                with self._available:
                    if self._closed or conn.in_transaction:
                        self._allocated -= 1
                        conn.close()
                    else:
                        self._idle.append(conn)
                    self._available.notify()

    @contextmanager
    def _transaction(self, begin: str) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            if conn.in_transaction:
                yield conn
                return
            conn.execute(begin)
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def write(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        """Start a write transaction or join the current transaction."""
        return self._transaction("BEGIN IMMEDIATE")

    def read(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        """Read several statements from a consistent snapshot."""
        return self._transaction("BEGIN")

    def execute(self, sql: str, params: Any = ()) -> WriteResult:
        with self.connection() as conn:
            return WriteResult(conn.execute(sql, params).rowcount)

    def one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        with self.connection() as conn:
            row: sqlite3.Row | None = conn.execute(sql, params).fetchone()
            return row

    def all(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(conn.execute(sql, params).fetchall())

    def scalar(self, sql: str, params: Any = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]

    def close(self) -> None:
        with self._available:
            self._closed = True
            conns, self._idle = self._idle, []
            self._allocated -= len(conns)
            self._available.notify_all()
        for conn in conns:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
