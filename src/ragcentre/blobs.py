"""Content-addressed blob store: ``blobs/ab/cd/<sha256>``.

Uploaded documents are stored as-is; parsed IR is stored gzip-compressed. Writes are atomic
(temporary file plus rename) and idempotent. Filenames from users are never used here.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ragcentre.hashing import sha256_bytes

_SHA = re.compile(r"^[0-9a-f]{64}$")


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sha: str) -> Path:
        if not _SHA.match(sha):
            raise ValueError(f"not a sha256 hex digest: {sha!r}")
        return self.root / sha[:2] / sha[2:4] / sha

    def put(self, data: bytes) -> str:
        sha = sha256_bytes(data)
        target = self.path(sha)
        if target.exists():
            # Refresh the age so garbage collection's grace period protects a re-used blob too.
            with contextlib.suppress(OSError):
                os.utime(target)
            return sha
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return sha

    def get(self, sha: str) -> bytes:
        try:
            return self.path(sha).read_bytes()
        except FileNotFoundError:
            raise KeyError(sha) from None

    def exists(self, sha: str) -> bool:
        return self.path(sha).exists()

    def size(self, sha: str) -> int:
        return self.path(sha).stat().st_size

    def delete(self, sha: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path(sha).unlink()

    def put_json_gz(self, obj: Any) -> str:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.put(gzip.compress(raw, mtime=0))

    def get_json_gz(self, sha: str) -> Any:
        return json.loads(gzip.decompress(self.get(sha)).decode("utf-8"))

    def iter_all(self) -> Iterator[str]:
        for path in self.root.glob("*/*/*"):
            if path.is_file() and _SHA.match(path.name):
                yield path.name
