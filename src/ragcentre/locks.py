"""Cross-process locks for document mutations and external index writes.

These locks never hold a metadata SQLite transaction across provider calls.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from ragcentre.hashing import sha256_text


@contextmanager
def document_lock(data_dir: Path, document_id: str) -> Iterator[None]:
    root = data_dir / "locks"
    root.mkdir(parents=True, exist_ok=True)
    with (root / sha256_text(document_id)).open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def index_catalog_lock(data_dir: Path) -> AbstractContextManager[None]:
    """Serialize index attachment and retirement across processes."""
    return document_lock(data_dir, "index-catalog")
