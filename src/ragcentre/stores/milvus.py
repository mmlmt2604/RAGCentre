"""Milvus vector store (optional extra ``ragcentre[milvus]``): Milvus Lite, standalone, or Zilliz Cloud.

Layout (all of it verified against pymilvus 3.0.3 with milvus-lite 3.2.2 and a Milvus v3.0.6
standalone server; see the ``DECISIONS.md`` notes in ``integration/t-milvus.md``):

* One Milvus collection per RAGCentre collection, named ``<prefix><logical name>``. Fields: ``id``
  (VARCHAR primary key), ``vector`` (FLOAT_VECTOR) and ``metadata`` (JSON, holding ``document_id``
  and every schema field). Filterable fields are *not* typed scalar fields: ``add_collection_field``
  exists on a standalone server but Milvus Lite answers UNIMPLEMENTED, so a typed layout could not
  gain a field on an existing collection everywhere. A JSON column accepts new fields for free,
  and the adapter filters it with ``stores.filter_milvus``.
* Collection metadata (dimension, metric, field types, index options) is kept by ``InStoreCatalog``
  in a side collection ``<prefix>ragcentre_catalog`` (VARCHAR key, VARCHAR entry, a dummy 2-d
  vector because Milvus refuses collections without a vector field). Milvus has no free-form
  collection metadata slot, and its collection properties accept fixed keys only.
* Queries and the catalog use ``consistency_level="Strong"`` (Milvus' default, Bounded, can miss
  recent writes). Counts use ``count(*)``: collection statistics are stale after writes and deletes
  on a standalone server.
* Scores: COSINE and IP are returned as the similarity and the raw dot product; L2 is returned as
  the *squared* distance, so the score is ``-sqrt(d)``.
* Calls on the query path (search, counts, describe, catalog reads) pass ``retry_times=0``:
  pymilvus retries RPC failures by itself, and the query path must not retry (section 20).
  Those calls therefore also carry no client-side deadline; pymilvus ties the deadline to its retry
  window. Writes and schema calls use the configured ``timeout`` and pymilvus' own retries, which
  is safe because upserts and deletes are idempotent.

Milvus Lite and the schema calls accept names that a real server rejects, so the adapter validates
names itself. ``pymilvus`` is imported only when a store is created.
"""

from __future__ import annotations

import contextlib
import math
import re
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import Filter
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
from ragcentre.stores.catalog import CatalogEntry, InStoreCatalog
from ragcentre.stores.filter_milvus import compile_milvus, string_literal
from ragcentre.stores.registry import VectorStoreType

ID_FIELD = "id"
VECTOR_FIELD = "vector"
META_FIELD = "metadata"
CATALOG_SUFFIX = "ragcentre_catalog"  # the catalog collection is <prefix> + this
MAX_DIM = 32768  # Milvus limit for FLOAT_VECTOR
MIN_DIM = 2  # real Milvus rejects 1-d vectors (Milvus Lite accepts them)
MAX_TOPK = 16384  # Milvus limit (Milvus Lite silently returns nothing above it)
MAX_COLLECTIONS = 65536
ID_MAX_LENGTH = 128
ENTRY_MAX_LENGTH = 65535
MAX_NAME = 255

_METRIC_TYPES: dict[Metric, str] = {"cosine": "COSINE", "l2": "L2", "dot": "IP"}
_PREFIX_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]{0,127})?$")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")
_DB_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9_]{0,254})?$")
_URL_SCHEMES = ("http://", "https://", "tcp://", "unix:")

# Passed to every call on the query path: no pymilvus retries (and so no client deadline).
_READ: dict[str, Any] = {"retry_times": 0, "retry_on_rate_limit": False}

BEST_CASE = Capabilities(
    metrics=frozenset({"cosine", "l2", "dot"}),
    max_batch=256,  # 256 x 32768 x 4 bytes stays well under the 64 MB insert limit
    max_dim=MAX_DIM,
    max_collections=MAX_COLLECTIONS,
)

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _target_lock(key: str) -> threading.RLock:
    """One lock per (uri, database, prefix), shared by every store object in this process."""
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _import() -> Any:
    return require_extra("pymilvus", store="milvus", extra="milvus", package="pymilvus")


def is_lite_uri(uri: str) -> bool:
    return not uri.startswith(_URL_SCHEMES)


def _is_not_found(exc: Exception) -> bool:
    return getattr(exc, "code", None) == 100  # ErrorCode.COLLECTION_NOT_FOUND (checked on Lite and a server)


class MilvusStore:
    capabilities: Capabilities

    def __init__(
        self,
        client: Any,
        *,
        target: str = "milvus",
        prefix: str = "",
        timeout: float = 30.0,
        index_type: Literal["FLAT", "HNSW"] = "FLAT",
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 200,
        hnsw_ef: int = 64,
    ) -> None:
        if not _PREFIX_RE.match(prefix):
            raise StoreError("prefix must start with a letter or underscore and use letters, digits and '_' only")
        self._mod = _import()
        self._client = client
        self._prefix = prefix
        self._timeout = timeout
        self._index_type = index_type
        self._hnsw_m = hnsw_m
        self._hnsw_ef_construction = hnsw_ef_construction
        self._hnsw_ef = hnsw_ef
        # Exact search and exact filtering only with FLAT; HNSW is approximate.
        self.capabilities = replace(BEST_CASE, filter_exact=index_type == "FLAT")
        self._catalog_name = self._checked_name(prefix + CATALOG_SUFFIX)
        self._lock = _target_lock(f"{target}|{prefix}")
        self._state = threading.Lock()
        self._entries: dict[str, CatalogEntry] = {}
        self._ready: set[str] = set()  # physical names loaded and checked in this process
        self._catalog_ready = False
        self._catalog = InStoreCatalog(
            self._catalog_read, self._catalog_write, self._catalog_remove, transaction=lambda: self._lock
        )

    @classmethod
    def connect(
        cls,
        *,
        uri: str,
        token: str | None = None,
        db_name: str = "",
        prefix: str = "",
        timeout: float = 30.0,
        index_type: Literal["FLAT", "HNSW"] = "FLAT",
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 200,
        hnsw_ef: int = 64,
    ) -> MilvusStore:
        mod = _import()
        kwargs: dict[str, Any] = {"uri": uri}
        if token:
            kwargs["token"] = token
        if db_name:
            kwargs["db_name"] = db_name
        try:
            client = mod.MilvusClient(**kwargs)
        except Exception as exc:
            raise StoreError(f"milvus: could not connect to {_display(uri)}: {exc}") from exc
        try:
            return cls(
                client,
                target=f"{uri}|{db_name}",
                prefix=prefix,
                timeout=timeout,
                index_type=index_type,
                hnsw_m=hnsw_m,
                hnsw_ef_construction=hnsw_ef_construction,
                hnsw_ef=hnsw_ef,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                client.close()
            raise

    # ---- helpers ------------------------------------------------------------------------------

    @staticmethod
    def _checked_name(physical: str) -> str:
        if not _NAME_RE.match(physical):
            raise StoreError(
                f"{physical!r} is not a valid Milvus collection name (letters, digits and '_', not starting "
                f"with a digit, at most {MAX_NAME} characters); shorten the prefix"
            )
        return physical

    def _physical(self, name: str) -> str:
        check_collection_name(name)
        if name == CATALOG_SUFFIX:
            raise StoreError(f"collection name {name!r} is reserved for the adapter's catalog")
        return self._checked_name(self._prefix + name)

    @property
    def _options(self) -> dict[str, Any]:
        """The index-shaping options a collection is built with (compared by every ensure)."""
        if self._index_type == "HNSW":
            return {
                "index_type": "HNSW",
                "hnsw_m": self._hnsw_m,
                "hnsw_ef_construction": self._hnsw_ef_construction,
            }
        return {"index_type": "FLAT"}

    def _call(self, what: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except StoreError:
            raise
        except Exception as exc:
            if isinstance(exc, self._mod.MilvusException) and _is_not_found(exc):
                raise CollectionNotFound(f"milvus: {what}: collection does not exist ({exc})") from exc
            raise StoreError(f"milvus: {what} failed: {exc}") from exc

    def _has(self, physical: str, **kw: Any) -> bool:
        return bool(self._call(f"check for collection {physical}", self._client.has_collection, physical, **kw))

    # ---- the catalog (a side collection) ------------------------------------------------------

    def _catalog_open(self, *, create: bool) -> bool:
        """True when the catalog collection exists and is loaded; creates it when ``create``."""
        if self._catalog_ready:
            return True
        name = self._catalog_name
        with self._lock:
            if not self._has(name, **_READ):
                if not create:
                    return False
                m = self._mod
                schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
                schema.add_field("name", m.DataType.VARCHAR, is_primary=True, max_length=MAX_NAME)
                schema.add_field("entry", m.DataType.VARCHAR, max_length=ENTRY_MAX_LENGTH)
                schema.add_field("vec", m.DataType.FLOAT_VECTOR, dim=MIN_DIM)  # Milvus needs a vector field
                idx = self._client.prepare_index_params()
                idx.add_index("vec", index_type="FLAT", metric_type="L2")
                try:
                    self._call(
                        f"create catalog collection {name}",
                        self._client.create_collection,
                        name,
                        schema=schema,
                        index_params=idx,
                        consistency_level="Strong",
                        timeout=self._timeout,
                    )
                except StoreError:
                    if not self._has(name):  # created concurrently by another process?
                        raise
            self._call(f"load catalog collection {name}", self._client.load_collection, name, timeout=self._timeout)
            self._catalog_ready = True
        return True

    def _catalog_read(self, name: str) -> str | None:
        if not self._catalog_open(create=False):
            return None
        rows = self._call(
            "read catalog",
            self._client.query,
            self._catalog_name,
            filter=f"name == {string_literal(name)}",
            output_fields=["entry"],
            consistency_level="Strong",
            **_READ,
        )
        return str(rows[0]["entry"]) if rows else None

    def _catalog_write(self, name: str, text: str) -> None:
        if len(text) > ENTRY_MAX_LENGTH:
            raise StoreError(f"collection {name} has too many filter fields to record in the catalog")
        self._catalog_open(create=True)
        self._call(
            "write catalog",
            self._client.upsert,
            self._catalog_name,
            [{"name": name, "entry": text, "vec": [0.0] * MIN_DIM}],
            timeout=self._timeout,
        )

    def _catalog_remove(self, name: str) -> None:
        if self._catalog_open(create=False):
            self._call(
                "update catalog",
                self._client.delete,
                self._catalog_name,
                filter=f"name == {string_literal(name)}",
                timeout=self._timeout,
            )

    # ---- per-collection state -----------------------------------------------------------------

    def _forget(self, name: str, physical: str | None = None) -> None:
        with self._state:
            self._entries.pop(name, None)
            if physical is not None:
                self._ready.discard(physical)

    def _require(self, name: str) -> tuple[str, CatalogEntry]:
        """The physical name and catalog entry of ``name``; loads the collection once per process."""
        physical = self._physical(name)
        with self._state:
            entry = self._entries.get(name)
            ready = physical in self._ready
        if entry is None:
            entry = self._catalog.get(name)
            if entry is None:
                raise CollectionNotFound(f"collection {name} does not exist")
            with self._state:
                self._entries[name] = entry
        if not ready:
            try:
                self._call(f"load collection {physical}", self._client.load_collection, physical, timeout=self._timeout)
            except CollectionNotFound:
                self._forget(name, physical)
                raise
            with self._state:
                self._ready.add(physical)
        return physical, entry

    def _create(self, physical: str, entry: CatalogEntry) -> None:
        m = self._mod
        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(ID_FIELD, m.DataType.VARCHAR, is_primary=True, max_length=ID_MAX_LENGTH)
        schema.add_field(VECTOR_FIELD, m.DataType.FLOAT_VECTOR, dim=entry.dim)
        schema.add_field(META_FIELD, m.DataType.JSON)
        idx = self._client.prepare_index_params()
        opts = entry.options
        if opts.get("index_type") == "HNSW":
            idx.add_index(
                VECTOR_FIELD,
                index_type="HNSW",
                metric_type=_METRIC_TYPES[entry.metric],
                params={"M": opts["hnsw_m"], "efConstruction": opts["hnsw_ef_construction"]},
            )
        else:
            idx.add_index(VECTOR_FIELD, index_type="FLAT", metric_type=_METRIC_TYPES[entry.metric])
        self._call(
            f"create collection {physical}",
            self._client.create_collection,
            physical,
            schema=schema,
            index_params=idx,
            consistency_level="Strong",
            timeout=self._timeout,
        )

    def _verify_physical(self, physical: str, entry: CatalogEntry) -> None:
        """Check an existing collection's real dimension and metric against the catalog entry."""
        desc = self._call(f"describe collection {physical}", self._client.describe_collection, physical, **_READ)
        dim = next((int(f["params"]["dim"]) for f in desc["fields"] if f["name"] == VECTOR_FIELD), None)
        index = self._call(
            f"describe index of {physical}", self._client.describe_index, physical, VECTOR_FIELD, **_READ
        )
        metric = next((m for m, t in _METRIC_TYPES.items() if t == index.get("metric_type")), None)
        if dim != entry.dim or metric != entry.metric:
            raise CollectionMismatch(
                f"milvus collection {physical} was not created by this catalog entry: it has dim={dim}, "
                f"metric={metric}; requested dim={entry.dim}, metric={entry.metric}"
            )

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        try:
            self._client.list_collections(timeout=self._timeout)
        except Exception as exc:
            raise StoreError(
                f"milvus is not reachable or this database/credential cannot list collections: {exc}"
            ) from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        if spec.dim < MIN_DIM:
            raise StoreError(
                f"Milvus needs at least {MIN_DIM} dimensions per vector; collection {spec.name} has {spec.dim}"
            )
        physical = self._physical(spec.name)
        with self._lock:
            exists = self._has(physical, timeout=self._timeout)
            if not exists:
                # A catalog entry without a collection is the residue of a crash or a failed create.
                self._catalog_open(create=True)
                self._catalog.drop(spec.name)
            had_entry = self._catalog.get(spec.name) is not None
            entry = self._catalog.ensure(spec, self._options)  # raises CollectionMismatch
            try:
                if not exists:
                    try:
                        self._create(physical, entry)
                    except StoreError:
                        if not self._has(physical, timeout=self._timeout):  # not created by a concurrent process
                            raise
                self._verify_physical(physical, entry)
                self._call(f"load collection {physical}", self._client.load_collection, physical, timeout=self._timeout)
            except BaseException:
                if not had_entry:
                    with contextlib.suppress(Exception):
                        self._catalog.drop(spec.name)
                raise
            finally:
                self._forget(spec.name)
            with self._state:
                self._ready.add(physical)

    def collection_info(self, name: str) -> CollectionInfo | None:
        physical = self._physical(name)
        entry = self._catalog.get(name)
        if entry is None or not self._has(physical, **_READ):
            return None
        rows = self._call(
            f"count {physical}",
            self._client.query,
            physical,
            filter="",
            output_fields=["count(*)"],
            consistency_level="Strong",
            **_READ,
        )
        return CollectionInfo(dim=entry.dim, metric=entry.metric, count=int(rows[0]["count(*)"]))

    def drop_collection(self, name: str) -> None:
        physical = self._physical(name)
        with self._lock:
            self._forget(name, physical)
            self._call(f"drop collection {physical}", self._client.drop_collection, physical, timeout=self._timeout)
            self._catalog.drop(name)

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        physical, entry = self._require(name)
        rows = []
        for rec in records:
            if len(rec.vector) != entry.dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {entry.dim}")
            if not all(map(math.isfinite, rec.vector)):  # Milvus Lite stores NaN without complaint
                raise StoreError(f"record {rec.id} has a non-finite vector component")
            meta = {
                k: float(v) if entry.fields.get(k) == "float" and not isinstance(v, bool) else v
                for k, v in rec.metadata.items()
            }
            rows.append({ID_FIELD: rec.id, VECTOR_FIELD: [float(x) for x in rec.vector], META_FIELD: meta})
        if rows:
            self._call(f"upsert into {physical}", self._client.upsert, physical, rows, timeout=self._timeout)

    def delete(self, name: str, ids: Sequence[str]) -> None:
        physical = self._physical(name)
        if not ids:
            return
        try:
            self._require(name)
        except CollectionNotFound:
            return
        expr = f"{ID_FIELD} in [{', '.join(string_literal(i) for i in ids)}]"
        with contextlib.suppress(CollectionNotFound):
            self._call(f"delete from {physical}", self._client.delete, physical, filter=expr, timeout=self._timeout)

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        physical, entry = self._require(name)
        if len(vector) != entry.dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {entry.dim}")
        if k < 1:
            return []
        if k > MAX_TOPK:
            raise StoreError(f"Milvus returns at most {MAX_TOPK} results per query; requested {k}")
        if not all(map(math.isfinite, vector)):
            raise StoreError("query vector has a non-finite component")
        params: dict[str, Any] = {"metric_type": _METRIC_TYPES[entry.metric]}
        if entry.options.get("index_type") == "HNSW":
            params["params"] = {"ef": max(self._hnsw_ef, k)}  # Milvus requires ef >= limit
        res = self._call(
            f"search {physical}",
            self._client.search,
            physical,
            data=[[float(x) for x in vector]],
            filter=compile_milvus(filter, META_FIELD),
            limit=k,
            anns_field=VECTOR_FIELD,
            search_params=params,
            consistency_level="Strong",
            **_READ,
        )
        # Milvus returns COSINE similarity and IP as they are, and L2 as the squared distance.
        return [Match(id=str(h["id"]), score=_score(entry.metric, float(h["distance"]))) for h in res[0]]

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()


def _score(metric: Metric, distance: float) -> float:
    return -math.sqrt(max(distance, 0.0)) if metric == "l2" else distance


def _display(uri: str) -> str:
    """A uri without any ``user:password@`` part, for messages."""
    return re.sub(r"//[^/@]*@", "//", uri)


# ---- registration ---------------------------------------------------------------------------


class MilvusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uri: str = Field(
        min_length=1,
        description="Milvus Lite file (a path ending in .db, relative to the data directory), "
        "a server URL such as http://localhost:19530, or a Zilliz Cloud endpoint",
    )
    db_name: str = Field(default="", description="Milvus database (default: the server's default database)")
    prefix: str = Field(default="", description="Prefix prepended to the physical Milvus collection names")
    timeout: float = Field(default=30.0, gt=0, description="Timeout in seconds for writes and schema calls")
    index_type: Literal["FLAT", "HNSW"] = Field(
        default="FLAT", description="FLAT is exact; HNSW is faster on large collections but approximate (set once)"
    )
    hnsw_m: int = Field(default=16, ge=2, le=2048, description="HNSW edges per node (set once)")
    hnsw_ef_construction: int = Field(default=200, ge=1, le=65535, description="HNSW build-time ef (set once)")
    hnsw_ef: int = Field(default=64, ge=1, le=32768, description="HNSW search-time ef (query time)")

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        if not _PREFIX_RE.match(v):
            raise ValueError("prefix must start with a letter or underscore and use letters, digits and '_' only")
        return v

    @field_validator("db_name")
    @classmethod
    def _check_db(cls, v: str) -> str:
        if not _DB_RE.match(v):
            raise ValueError("db_name must start with a letter and use letters, digits and '_' only")
        return v

    @model_validator(mode="after")
    def _check_uri(self) -> MilvusConfig:
        if is_lite_uri(self.uri) and not self.uri.endswith(".db"):
            raise ValueError("a Milvus Lite uri must be a file path ending in .db; servers need an http(s):// URL")
        return self


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> MilvusStore:
    assert isinstance(config, MilvusConfig)
    uri = config.uri
    if is_lite_uri(uri):
        path = Path(uri)
        if not path.is_absolute():
            path = data_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        uri = str(path)
    return MilvusStore.connect(
        uri=uri,
        token=secrets.get("token") or None,
        db_name=config.db_name,
        prefix=config.prefix,
        timeout=config.timeout,
        index_type=config.index_type,
        hnsw_m=config.hnsw_m,
        hnsw_ef_construction=config.hnsw_ef_construction,
        hnsw_ef=config.hnsw_ef,
    )


STORE_TYPE = VectorStoreType(
    type="milvus",
    label="Milvus",
    description="Milvus standalone or cluster, Zilliz Cloud, or Milvus Lite (a local file). "
    "Needs the optional 'milvus' extra.",
    config_model=MilvusConfig,
    secret_keys=("token",),
    capabilities=BEST_CASE,
    factory=_factory,
    immutable_fields=("index_type", "hnsw_m", "hnsw_ef_construction"),
)

__all__ = ["BEST_CASE", "STORE_TYPE", "MilvusConfig", "MilvusStore", "compile_milvus"]
