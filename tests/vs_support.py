"""A configurable in-memory vector store for testing the V1 foundations (catalog, capabilities,
contract hooks). It is brute force over Python dicts, with its collection metadata in a ``Catalog``.

The knobs make it misbehave on purpose, so tests can show that the contract suite notices.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict

from ragcentre.models.filters import Filter, matches
from ragcentre.stores.base import (
    METRICS,
    Capabilities,
    CollectionInfo,
    CollectionNotFound,
    CollectionSpec,
    Match,
    Record,
    StoreError,
    check_collection_name,
    check_spec,
)
from ragcentre.stores.catalog import Catalog
from ragcentre.testing.vector_store_contract import brute_score


class MemoryStore:
    def __init__(
        self,
        catalog: Catalog,
        *,
        capabilities: Capabilities | None = None,
        options: dict[str, Any] | None = None,
        delay: float = 0.0,  # writes become visible this many seconds later ("eventual")
        drop_every: int | None = None,  # filtered queries lose every n-th ranked result
        score_offset: float = 0.0,  # added to every score (a lossy score transform)
        enforce_max_dim: bool = True,
    ) -> None:
        self.capabilities = capabilities or Capabilities(metrics=frozenset(METRICS), max_batch=50)
        self._catalog = catalog
        self._options = options or {}
        self._delay = delay
        self._drop_every = drop_every
        self._score_offset = score_offset
        self._enforce_max_dim = enforce_max_dim
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Record]] = {}
        self._pending: list[tuple[float, str, str, Any]] = []  # (due, collection, kind, payload)
        self.closed = False

    # ---- eventual consistency -------------------------------------------------------------------

    def _apply_due(self) -> None:
        now = time.monotonic()
        with self._lock:
            due = [p for p in self._pending if p[0] <= now]
            self._pending = [p for p in self._pending if p[0] > now]
            for _, name, kind, payload in due:
                data = self._data.get(name)
                if data is None:
                    continue
                if kind == "upsert":
                    for rec in payload:
                        data[rec.id] = rec
                else:
                    for rid in payload:
                        data.pop(rid, None)

    def pending(self) -> int:
        self._apply_due()
        return len(self._pending)

    def _write(self, name: str, kind: str, payload: Sequence[Any]) -> None:
        with self._lock:
            self._pending.append((time.monotonic() + self._delay, name, kind, list(payload)))
        self._apply_due()

    # ---- VectorStore ----------------------------------------------------------------------------

    def healthcheck(self) -> None:
        return None

    def ensure_collection(self, spec: CollectionSpec) -> None:
        caps = self.capabilities if self._enforce_max_dim else replace(self.capabilities, max_dim=None)
        check_spec(spec, caps)
        self._catalog.ensure(spec, self._options)
        with self._lock:
            self._data.setdefault(spec.name, {})

    def collection_info(self, name: str) -> CollectionInfo | None:
        check_collection_name(name)
        entry = self._catalog.get(name)
        if entry is None:
            return None
        self._apply_due()
        with self._lock:
            return CollectionInfo(entry.dim, entry.metric, len(self._data.get(name, {})))

    def drop_collection(self, name: str) -> None:
        check_collection_name(name)
        with self._lock:
            self._data.pop(name, None)
            self._pending = [p for p in self._pending if p[1] != name]
        self._catalog.drop(name)

    def _require(self, name: str) -> Any:
        check_collection_name(name)
        entry = self._catalog.get(name)
        if entry is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        with self._lock:
            self._data.setdefault(name, {})
        return entry

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        entry = self._require(name)
        for rec in records:
            if len(rec.vector) != entry.dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection has {entry.dim}")
        self._write(name, "upsert", records)

    def delete(self, name: str, ids: Sequence[str]) -> None:
        if self._catalog.get(name) is None or not ids:
            return
        self._write(name, "delete", ids)

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        entry = self._require(name)
        self._apply_due()
        with self._lock:
            records = list(self._data[name].values())
        scored = [
            (r.id, brute_score(entry.metric, r.vector, vector) + self._score_offset)
            for r in records
            if matches(filter, r.metadata)
        ]
        scored.sort(key=lambda x: (-x[1], x[0]))
        top = scored[:k]
        if filter is not None and self._drop_every:
            n = self._drop_every
            top = [item for i, item in enumerate(top) if i % n != n - 1]
        return [Match(i, s) for i, s in top]

    def close(self) -> None:
        self.closed = True


class FakeStoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = "http://localhost:1"  # a connection field: mutable
    quantization: str = "none"  # an index-shaping field: immutable
    max_dim: int | None = None
