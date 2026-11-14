"""Qdrant vector store (optional extra ``ragcentre[qdrant]``).

Each collection has a single unnamed dense vector. The collection's dimension and distance come
from Qdrant itself; the filterable fields and their types are kept in the collection's own
``metadata`` under ``ragcentre``. A keyword/integer/float/bool payload index is created per field
and for ``document_id`` (the client's local mode ignores payload indexes, which is harmless).

``qdrant_client`` is imported only when a store is created, so ``import ragcentre`` works without
the extra.
"""

from __future__ import annotations

import contextlib
import threading
import warnings
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import And, Cond, FieldType, Filter, Or
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
from ragcentre.stores.registry import VectorStoreType

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

META_KEY = "ragcentre"
_DISTANCES: dict[Metric, str] = {"cosine": "Cosine", "l2": "Euclid", "dot": "Dot"}
_INDEX_TYPES: dict[FieldType, str] = {"str": "keyword", "int": "integer", "float": "float", "bool": "bool"}


def _import() -> Any:
    require_extra("qdrant_client", store="qdrant", extra="qdrant", package="qdrant-client")
    from qdrant_client import QdrantClient, models

    return QdrantClient, models


def _is_not_found(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 404:
        return True
    return isinstance(exc, ValueError) and "not found" in str(exc).lower()


# ---- filter translation ---------------------------------------------------------------------


def translate_filter(f: Filter, fields: dict[str, FieldType], models: Any) -> Any:
    """Translate a parsed filter to a ``models.Filter``.

    ``$eq`` → match value; ``$ne`` → the same under ``must_not``; ``$in`` → match any; ``$nin`` →
    match except; range operators → range; ``$and`` → ``must``; ``$or`` → ``should``. Qdrant's
    match conditions take strings, integers and booleans only, so float fields use closed ranges
    for equality and membership.
    """
    if isinstance(f, And):
        return models.Filter(must=[translate_filter(item, fields, models) for item in f.items])
    if isinstance(f, Or):
        return models.Filter(should=[translate_filter(item, fields, models) for item in f.items])
    return _condition(f, fields.get(f.field, "str"), models)


def _condition(c: Cond, ftype: FieldType, models: Any) -> Any:
    def cond(**kw: Any) -> Any:
        return models.FieldCondition(key=c.field, **kw)

    def point(v: Any) -> Any:  # equality for floats
        return cond(range=models.Range(gte=float(v), lte=float(v)))

    numeric_float = ftype == "float"
    if c.op == "$eq":
        return models.Filter(must=[point(c.value) if numeric_float else cond(match=models.MatchValue(value=c.value))])
    if c.op == "$ne":
        inner = point(c.value) if numeric_float else cond(match=models.MatchValue(value=c.value))
        return models.Filter(must_not=[inner])
    if c.op in ("$gt", "$gte", "$lt", "$lte"):
        return models.Filter(must=[cond(range=models.Range(**{c.op[1:]: c.value}))])
    assert isinstance(c.value, tuple)
    values = list(c.value)
    if numeric_float:
        points = [point(v) for v in values]
        return models.Filter(should=points) if c.op == "$in" else models.Filter(must_not=points)
    if ftype == "bool":  # MatchAny and MatchExcept take strings and integers only
        exact = [cond(match=models.MatchValue(value=v)) for v in values]
        return models.Filter(should=exact) if c.op == "$in" else models.Filter(must_not=exact)
    if c.op == "$in":
        return models.Filter(must=[cond(match=models.MatchAny(any=values))])
    return models.Filter(must=[cond(match=models.MatchExcept(**{"except": values}))])


# ---- store ----------------------------------------------------------------------------------


class QdrantStore:
    capabilities: ClassVar[Capabilities] = Capabilities(metrics=frozenset({"cosine", "l2", "dot"}), max_batch=256)

    def __init__(self, client: QdrantClient, *, local: bool = False) -> None:
        _, self._models = _import()
        self._client = client
        # The embedded local mode is plain Python state; serialise access to it.
        self._lock: threading.RLock | None = threading.RLock() if local else None
        self._meta_cache: dict[str, tuple[int, Metric, dict[str, FieldType]]] = {}
        self._cache_lock = threading.Lock()

    @classmethod
    def connect(cls, *, url: str | None = None, path: str | None = None, api_key: str | None = None) -> QdrantStore:
        client_cls, _ = _import()
        if path is not None:
            location = ":memory:" if path == ":memory:" else None
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                client = client_cls(location=location, path=None if location else path)
            return cls(client, local=True)
        return cls(client_cls(url=url, api_key=api_key), local=False)

    @contextlib.contextmanager
    def _guard(self) -> Iterator[None]:
        if self._lock is None:
            yield
        else:
            with self._lock:
                yield

    # ---- helpers ------------------------------------------------------------------------------

    def _read_meta(self, name: str) -> tuple[int, Metric, dict[str, FieldType]] | None:
        with self._guard():
            try:
                info = self._client.get_collection(name)
            except Exception as exc:
                if _is_not_found(exc):
                    return None
                raise StoreError(f"qdrant: could not read collection {name}: {exc}") from exc
        params = info.config.params.vectors
        if params is None or isinstance(params, dict):
            raise StoreError(f"qdrant collection {name} was not created by RAGCentre (named vectors)")
        metric = next(m for m, d in _DISTANCES.items() if d == params.distance.value)
        fields = dict(((info.config.metadata or {}).get(META_KEY) or {}).get("fields") or {})
        return int(params.size), metric, fields

    def _meta(self, name: str) -> tuple[int, Metric, dict[str, FieldType]]:
        check_collection_name(name)
        with self._cache_lock:
            cached = self._meta_cache.get(name)
        if cached is not None:
            return cached
        meta = self._read_meta(name)
        if meta is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        with self._cache_lock:
            self._meta_cache[name] = meta
        return meta

    def _forget(self, name: str) -> None:
        with self._cache_lock:
            self._meta_cache.pop(name, None)

    def _call(self, name: str, what: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        with self._guard():
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if _is_not_found(exc):
                    self._forget(name)
                    raise CollectionNotFound(f"collection {name} does not exist") from exc
                raise StoreError(f"qdrant: {what} on {name} failed: {exc}") from exc

    def _create_indexes(self, name: str, fields: dict[str, FieldType]) -> None:
        m = self._models
        for fname, ftype in {"document_id": "str", **fields}.items():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # local mode warns that indexes have no effect
                self._call(name, "create payload index", self._client.create_payload_index, name, fname,
                           m.PayloadSchemaType(_INDEX_TYPES[ftype]))  # fmt: skip

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        with self._guard():
            try:
                self._client.get_collections()
            except Exception as exc:
                raise StoreError(f"qdrant is not reachable: {exc}") from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        m = self._models
        meta = self._read_meta(spec.name)
        if meta is None:
            try:
                with self._guard():
                    self._client.create_collection(
                        spec.name,
                        vectors_config=m.VectorParams(size=spec.dim, distance=m.Distance(_DISTANCES[spec.metric])),
                        metadata={META_KEY: {"fields": dict(spec.fields)}},
                    )
            except Exception as exc:
                meta = self._read_meta(spec.name)  # created concurrently?
                if meta is None:
                    raise StoreError(f"qdrant: could not create collection {spec.name}: {exc}") from exc
            else:
                self._forget(spec.name)
                self._create_indexes(spec.name, dict(spec.fields))
                return
        dim, metric, fields = meta
        if dim != spec.dim or metric != spec.metric:
            raise CollectionMismatch(
                f"collection {spec.name} exists with dim={dim}, metric={metric}; "
                f"requested dim={spec.dim}, metric={spec.metric}"
            )
        for fname, ftype in spec.fields.items():
            if fname in fields and fields[fname] != ftype:
                raise CollectionMismatch(f"field {fname} is {fields[fname]} in {spec.name}, not {ftype}")
        merged = {**fields, **spec.fields}
        if merged != fields:
            self._call(spec.name, "update collection", self._client.update_collection, spec.name,
                       metadata={META_KEY: {"fields": merged}})  # fmt: skip
            self._create_indexes(spec.name, {k: v for k, v in merged.items() if k not in fields})
        self._forget(spec.name)

    def collection_info(self, name: str) -> CollectionInfo | None:
        check_collection_name(name)
        meta = self._read_meta(name)
        if meta is None:
            return None
        count = self._call(name, "count", self._client.count, name, exact=True).count
        return CollectionInfo(dim=meta[0], metric=meta[1], count=int(count))

    def drop_collection(self, name: str) -> None:
        check_collection_name(name)
        self._forget(name)
        with self._guard():
            try:
                self._client.delete_collection(name)
            except Exception as exc:
                if not _is_not_found(exc):
                    raise StoreError(f"qdrant: could not drop collection {name}: {exc}") from exc

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        dim, _, fields = self._meta(name)
        points = []
        for rec in records:
            if len(rec.vector) != dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {dim}")
            payload = {k: float(v) if fields.get(k) == "float" else v for k, v in rec.metadata.items()}
            points.append(self._models.PointStruct(id=rec.id, vector=list(rec.vector), payload=payload))
        if points:
            self._call(name, "upsert", self._client.upsert, name, points=points, wait=True)

    def delete(self, name: str, ids: Sequence[str]) -> None:
        check_collection_name(name)
        if not ids:
            return
        try:
            self._meta(name)
        except CollectionNotFound:
            return
        selector = self._models.PointIdsList(points=list(ids))
        with contextlib.suppress(CollectionNotFound):
            self._call(name, "delete", self._client.delete, name, points_selector=selector, wait=True)

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        dim, metric, fields = self._meta(name)
        if len(vector) != dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {dim}")
        if k < 1:
            return []
        query_filter = None if filter is None else translate_filter(filter, fields, self._models)
        result = self._call(name, "query", self._client.query_points, name, query=list(vector), limit=k,
                            query_filter=query_filter, with_payload=False, with_vectors=False)  # fmt: skip
        sign = -1.0 if metric == "l2" else 1.0  # Qdrant reports Euclidean distance, not similarity
        return [Match(id=str(p.id), score=sign * float(p.score)) for p in result.points]

    def close(self) -> None:
        with self._guard(), contextlib.suppress(Exception):
            self._client.close()


# ---- registration ---------------------------------------------------------------------------


class QdrantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, description="Qdrant server URL, e.g. http://localhost:6333")
    path: str | None = Field(
        default=None, description="Directory for the client's embedded local mode, or ':memory:' (instead of url)"
    )

    @model_validator(mode="after")
    def _one_location(self) -> QdrantConfig:
        if (self.url is None) == (self.path is None):
            raise ValueError("set exactly one of url or path")
        return self


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> QdrantStore:
    assert isinstance(config, QdrantConfig)
    path = config.path
    if path is not None and path != ":memory:" and not Path(path).is_absolute():
        path = str(data_dir / path)
    return QdrantStore.connect(url=config.url, path=path, api_key=secrets.get("api_key") or None)


STORE_TYPE = VectorStoreType(
    type="qdrant",
    label="Qdrant",
    description="Qdrant server, or the client's embedded local mode. Needs the optional 'qdrant' extra.",
    config_model=QdrantConfig,
    secret_keys=("api_key",),
    capabilities=QdrantStore.capabilities,
    factory=_factory,
)
