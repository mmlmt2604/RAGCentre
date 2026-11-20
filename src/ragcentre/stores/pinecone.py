"""Pinecone classic dense indexes (SDK 7, optional extra ``ragcentre[pinecone]``).

Each logical collection owns an index, allowing independent dimensions and metrics. Index names
are deterministic hashes of logical names. Never point this provider at indexes managed by
another application. Field types and deployment options live in LocalCatalog; back it up with
the core database. Vector reads are eventually consistent in Pinecone Cloud.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import And, Filter, Or, to_json
from ragcentre.stores.base import (
    Capabilities,
    CollectionInfo,
    CollectionNotFound,
    CollectionSpec,
    Match,
    Metric,
    Record,
    StoreError,
    check_collection_name,
    check_spec,
    require_extra,
)
from ragcentre.stores.catalog import CatalogEntry, LocalCatalog
from ragcentre.stores.registry import VectorStoreType

_METRICS: dict[Metric, str] = {"cosine": "cosine", "l2": "euclidean", "dot": "dotproduct"}
_CAPS = Capabilities(
    metrics=frozenset(_METRICS), max_batch=100, max_dim=20000, consistency="eventual", filter_exact=False
)


def _import() -> Any:
    return require_extra("pinecone", store="pinecone", extra="pinecone", package="pinecone")


def translate_filter(f: Filter) -> dict[str, Any]:
    if isinstance(f, (And, Or)):
        return {"$and" if isinstance(f, And) else "$or": [translate_filter(x) for x in f.items]}
    # Pinecone accepts boolean equality, but $in/$nin only accept strings and numbers.
    if f.op in ("$in", "$nin") and isinstance(f.value, tuple) and any(isinstance(v, bool) for v in f.value):
        joiner, op = ("$or", "$eq") if f.op == "$in" else ("$and", "$ne")
        return {joiner: [{f.field: {op: v}} for v in f.value]}
    return to_json(f)


def _missing(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 404


class PineconeStore:
    capabilities = _CAPS

    def __init__(
        self,
        client: Any,
        catalog: LocalCatalog,
        *,
        index_prefix: str = "rc",
        cloud: str = "aws",
        region: str = "us-east-1",
        ready_timeout: int = 120,
        local_http: bool = False,
    ) -> None:
        self._client = client
        self._catalog = catalog
        self._prefix = index_prefix
        self._cloud = cloud
        self._region = region
        self._ready_timeout = ready_timeout
        self._local_http = local_http
        self._indexes: dict[str, Any] = {}
        self._lock = threading.RLock()

    @classmethod
    def connect(
        cls,
        *,
        data_dir: Path,
        api_key: str | None,
        host: str | None = None,
        index_prefix: str = "rc",
        cloud: str = "aws",
        region: str = "us-east-1",
        ready_timeout: int = 120,
    ) -> PineconeStore:
        p = _import()
        if not api_key:
            raise StoreError("pinecone: configure the api_key secret (Pinecone Local accepts any nonempty value)")
        try:
            client = p.Pinecone(api_key=api_key, **({"host": host} if host else {}))
            # SDK 7 shares this configuration with indexes but rejects constructor injection.
            # Set it before creating any transports so retrieval has no hidden retries.
            client._openapi_config.retries = 0
            catalog = LocalCatalog(data_dir, "pinecone", f"{host or 'https://api.pinecone.io'}/{index_prefix}")
        except Exception as exc:
            raise StoreError("pinecone: could not open the configured provider") from exc
        return cls(
            client,
            catalog,
            index_prefix=index_prefix,
            cloud=cloud,
            region=region,
            ready_timeout=ready_timeout,
            local_http=bool(host and host.startswith("http://")),
        )

    def _physical(self, name: str) -> str:
        check_collection_name(name)
        return self._prefix + "-" + hashlib.sha256(name.encode()).hexdigest()[:32]

    def _describe(self, name: str) -> Any:
        try:
            return self._client.describe_index(self._physical(name))
        except Exception as exc:
            if _missing(exc):
                return None
            raise StoreError(f"pinecone: could not describe collection {name}") from exc

    def _entry(self, name: str) -> CatalogEntry:
        self._physical(name)
        entry = self._catalog.get(name)
        if entry is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        return entry

    def _index(self, name: str) -> Any:
        with self._lock:
            if name not in self._indexes:
                desc = self._describe(name)
                if desc is None:
                    raise CollectionNotFound(f"collection {name} does not exist")
                host = desc.host
                if self._local_http and "://" not in host:
                    host = "http://" + host
                self._indexes[name] = self._client.Index(host=host)
            return self._indexes[name]

    def _wrap(self, name: str, action: str, exc: Exception) -> StoreError:
        if isinstance(exc, CollectionNotFound):
            return exc
        if _missing(exc):
            with self._lock:
                index = self._indexes.pop(name, None)
                if index is not None:
                    index.close()
            return CollectionNotFound(f"collection {name} does not exist")
        return StoreError(f"pinecone: {action} on {name} failed")

    def healthcheck(self) -> None:
        try:
            self._client.list_indexes()
        except Exception as exc:
            raise StoreError("pinecone is not reachable or the API key is invalid") from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        with self._lock:
            desc = self._describe(spec.name)
            if desc is not None and self._catalog.get(spec.name) is None:
                raise StoreError(
                    f"pinecone: collection {spec.name} has no local catalog; restore its catalog or rebuild"
                )
            if desc is not None and (desc.dimension != spec.dim or desc.metric != _METRICS[spec.metric]):
                raise CollectionMismatch(
                    f"pinecone collection {spec.name} has a different physical dimension or metric"
                )
            self._catalog.ensure(spec, {"cloud": self._cloud, "region": self._region})
            if desc is None:
                p = _import()
                try:
                    self._client.create_index(
                        name=self._physical(spec.name),
                        dimension=spec.dim,
                        metric=_METRICS[spec.metric],
                        spec=p.ServerlessSpec(cloud=self._cloud, region=self._region),
                        timeout=self._ready_timeout,
                    )
                except Exception as exc:
                    # A simultaneous ensure may have created it. Never swallow other failures.
                    if getattr(exc, "status", None) != 409:
                        raise StoreError(f"pinecone: could not create collection {spec.name}") from exc
                desc = self._describe(spec.name)
            if desc is None:
                raise StoreError(f"pinecone: collection {spec.name} disappeared during creation")
            if (desc.dimension, desc.metric) != (spec.dim, _METRICS[spec.metric]):
                raise CollectionMismatch(
                    f"pinecone collection {spec.name} has a different physical dimension or metric"
                )
            self._index(spec.name)

    def collection_info(self, name: str) -> CollectionInfo | None:
        self._physical(name)
        entry = self._catalog.get(name)
        if entry is None:
            return None
        desc = self._describe(name)
        if desc is None:
            return None
        metric = next((k for k, v in _METRICS.items() if v == desc.metric), None)
        if metric is None:
            raise StoreError(f"pinecone: unsupported physical metric {desc.metric}")
        try:
            stats = self._index(name).describe_index_stats()
            return CollectionInfo(int(desc.dimension), metric, int(stats.total_vector_count))
        except Exception as exc:
            raise self._wrap(name, "count", exc) from exc

    def drop_collection(self, name: str) -> None:
        physical = self._physical(name)
        # Only delete indexes owned by this catalog, including a pending partial creation.
        if self._catalog.get(name) is None:
            return
        try:
            self._client.delete_index(physical, timeout=self._ready_timeout)
        except Exception as exc:
            if not _missing(exc):
                raise self._wrap(name, "drop", exc) from exc
        with self._lock:
            index = self._indexes.pop(name, None)
            if index is not None:
                index.close()
        self._catalog.drop(name)

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch exceeds max_batch {self.capabilities.max_batch}")
        entry = self._entry(name)
        for rec in records:
            if len(rec.vector) != entry.dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; expected {entry.dim}")
        if not records:
            return
        try:
            self._index(name).upsert(
                vectors=[{"id": r.id, "values": list(r.vector), "metadata": r.metadata} for r in records]
            )
        except Exception as exc:
            raise self._wrap(name, "upsert", exc) from exc

    def delete(self, name: str, ids: Sequence[str]) -> None:
        self._physical(name)
        if not ids or self._catalog.get(name) is None:
            return
        try:
            for start in range(0, len(ids), 1000):
                self._index(name).delete(ids=list(ids[start : start + 1000]))
        except CollectionNotFound:
            return
        except Exception as exc:
            if not _missing(exc):
                raise self._wrap(name, "delete", exc) from exc

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        entry = self._entry(name)
        if len(vector) != entry.dim:
            raise StoreError(f"query dimension {len(vector)} differs from {entry.dim}")
        if k < 1:
            return []
        try:
            result = self._index(name).query(
                vector=list(vector),
                top_k=min(k, 10000),
                include_metadata=False,
                include_values=False,
                _request_timeout=30,
                **({"filter": translate_filter(filter)} if filter is not None else {}),
            )
            hits = [
                Match(str(m.id), -math.sqrt(max(float(m.score), 0.0)) if entry.metric == "l2" else float(m.score))
                for m in result.matches
            ]
            return sorted(hits, key=lambda m: -m.score)
        except Exception as exc:
            raise self._wrap(name, "query", exc) from exc

    def close(self) -> None:
        for index in self._indexes.values():
            index.close()
        self._indexes.clear()
        # SDK 7 has no top-level close(); release its shared control-plane transport.
        self._client.db._index_api.api_client.close()
        self._catalog.close()


class PineconeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str | None = Field(
        default=None, description="Control-plane URL; empty for Cloud, http://localhost:5080 for Local"
    )
    index_prefix: str = Field(
        default="rc",
        pattern=r"^[a-z][a-z0-9-]{0,10}$",
        description="Prefix for owned indexes; isolate apps/accounts sharing a data directory",
    )
    cloud: Literal["aws", "gcp", "azure"] = "aws"
    region: str = Field(default="us-east-1", min_length=1)
    ready_timeout: int = Field(default=120, ge=1, le=600, description="Index creation/deletion timeout, seconds")

    @field_validator("host")
    @classmethod
    def _host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("use an HTTP(S) origin without credentials, path, query or fragment")
        return value.rstrip("/")


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> PineconeStore:
    assert isinstance(config, PineconeConfig)
    return PineconeStore.connect(data_dir=data_dir, api_key=secrets.get("api_key"), **config.model_dump())


STORE_TYPE = VectorStoreType(
    type="pinecone",
    label="Pinecone",
    description="Pinecone Cloud or Local; one dense index per collection. Needs the 'pinecone' extra.",
    config_model=PineconeConfig,
    secret_keys=("api_key",),
    capabilities=_CAPS,
    factory=_factory,
    immutable_fields=("cloud", "region"),
)
