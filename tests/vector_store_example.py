"""Runnable Python adapter example for docs/protocols/vector-store-adapter-guide.md.

A deliberately ephemeral, exact store. Use a persistent database for actual ingestion.
No pytest or vendor client imports: copy this file into your adapter package to start.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ragcentre.models.filters import Filter, matches
from ragcentre.stores.base import (
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
from ragcentre.stores.catalog import CatalogEntry, InStoreCatalog
from ragcentre.stores.registry import VectorStoreType
from ragcentre.vectors import cosine_similarity, dot, l2_distance


class ExampleStore:
    def __init__(self, max_dim: int = 4096) -> None:
        self.capabilities = Capabilities(frozenset({"cosine", "dot", "l2"}), 100, max_dim=max_dim)
        self._lock = threading.RLock()
        self._metadata: dict[str, str] = {}
        self._records: dict[str, dict[str, Record]] = {}
        self._catalog = InStoreCatalog(self._metadata.get, self._metadata.__setitem__, self._remove_metadata)

    def _remove_metadata(self, name: str) -> None:
        self._metadata.pop(name, None)

    def _require(self, name: str) -> CatalogEntry:
        check_collection_name(name)
        entry = self._catalog.get(name)
        if entry is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        return entry

    def healthcheck(self) -> None:
        pass

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        with self._lock:
            self._catalog.ensure(spec)
            self._records.setdefault(spec.name, {})

    def collection_info(self, name: str) -> CollectionInfo | None:
        check_collection_name(name)
        with self._lock:
            entry = self._catalog.get(name)
            if entry is None:
                return None
            return CollectionInfo(entry.dim, entry.metric, len(self._records[name]))

    def drop_collection(self, name: str) -> None:
        check_collection_name(name)
        with self._lock:
            self._records.pop(name, None)
            self._catalog.drop(name)

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        with self._lock:
            entry = self._require(name)
            if len(records) > self.capabilities.max_batch:
                raise StoreError("upsert exceeds max_batch")
            if any(len(r.vector) != entry.dim for r in records):
                raise StoreError("record vector dimension differs from collection")
            for r in records:
                self._records[name][r.id] = Record(r.id, list(r.vector), dict(r.metadata))

    def delete(self, name: str, ids: Sequence[str]) -> None:
        check_collection_name(name)
        with self._lock:
            data = self._records.get(name, {})
            for rid in ids:
                data.pop(rid, None)

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        with self._lock:
            entry = self._require(name)
            if len(vector) != entry.dim:
                raise StoreError("query vector dimension differs from collection")
            if k < 1:
                return []
            result = []
            for rec in self._records[name].values():
                if not matches(filter, rec.metadata):
                    continue  # filter BEFORE ranking and top-k
                if entry.metric == "cosine":
                    score = cosine_similarity(rec.vector, vector)
                elif entry.metric == "l2":
                    score = -l2_distance(rec.vector, vector)
                else:
                    score = dot(rec.vector, vector)
                result.append(Match(rec.id, score))
            return sorted(result, key=lambda m: (-m.score, m.id))[:k]

    def close(self) -> None:
        with self._lock:
            self._records.clear()
            self._metadata.clear()


class ExampleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_dim: int = Field(default=4096, ge=1)


def factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> ExampleStore:
    assert isinstance(config, ExampleConfig)
    return ExampleStore(max_dim=config.max_dim)


STORE_TYPE = VectorStoreType(
    type="example_memory",
    label="Example memory store",
    description="Ephemeral exact search, for adapter development only.",
    config_model=ExampleConfig,
    secret_keys=(),
    capabilities=Capabilities(frozenset({"cosine", "dot", "l2"}), 100),
    factory=factory,
)
