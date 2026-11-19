"""Chroma vector store (optional extra ``ragcentre[chroma]``).

Three deployment modes share one adapter:

* ``local``  — ``PersistentClient`` on a directory (default ``<data dir>/chroma``);
* ``http``   — ``HttpClient`` against a Chroma server or auth proxy;
* ``cloud``  — ``CloudClient`` against Chroma Cloud (``api_key`` secret; 300-record writes).

The distance space and the immutable HNSW options are set when a collection is created
(``configuration.hnsw.space`` / ``max_neighbors`` / ``ef_construction``). The collection's
dimension and filterable fields are kept in its metadata (``rc_dim``, ``rc_fields``); the
immutable HNSW options are recorded there too (``rc_hnsw_max_neighbors``,
``rc_hnsw_ef_construction``) so ``ensure_collection`` raises ``CollectionMismatch`` when they
change (Decision 5). ``ef_search`` is query-time mutable and is applied with
``Collection.modify``. Collections are always opened with ``embedding_function=None``: RAGCentre
supplies every vector, and Chroma must never fall back to its default embedding model.

Scores: Chroma returns distances. cosine → 1 − d; inner product (Chroma's ``ip`` is 1 − dot) →
1 − d; l2 is **squared** Euclidean distance → −√d.

Chroma's ``where`` is nearly the filter language itself. It needs a single top-level key, so
multi-condition filters become an explicit ``$and``; the parsed filter never has a one-operand
``$and``/``$or``. Float fields are written and filtered as floats because Chroma requires every
value in an ``$in``/``$nin`` list to share one type.

``chromadb`` is imported only when a store is created, so ``import ragcentre`` works without the
extra.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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

DEFAULT_MAX_BATCH = 1000
CLOUD_MAX_BATCH = 300  # Chroma Cloud caps each write at 300 records
DEFAULT_CLOUD_HOST = "api.trychroma.com"
_SPACES: dict[Metric, str] = {"cosine": "cosine", "l2": "l2", "dot": "ip"}

# Chroma's collection-name rules (chromadb.api.segment.check_index_name): 3-63 characters,
# alphanumeric at both ends, otherwise [a-zA-Z0-9._-], no "..", and not an IPv4 literal.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]$")
_IPV4_RE = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")


def _import() -> Any:
    return require_extra("chromadb", store="chroma", extra="chroma", package="chromadb")


def _client_settings(chromadb: Any, **extra: Any) -> Any:
    return chromadb.config.Settings(anonymized_telemetry=False, **extra)


def _score(metric: Metric, distance: float) -> float:
    if metric == "l2":
        return -math.sqrt(max(distance, 0.0))
    return 1.0 - distance


def _value(v: Any, ftype: FieldType) -> Any:
    return float(v) if ftype == "float" and not isinstance(v, bool) else v


def _check_chroma_name(name: str) -> str:
    if not 3 <= len(name) <= 63:
        raise StoreError(f"invalid chroma collection name {name!r}: must be 3-63 characters")
    if not _NAME_RE.match(name):
        raise StoreError(
            f"invalid chroma collection name {name!r}: must start and end with an alphanumeric "
            "character and contain only [a-zA-Z0-9._-]"
        )
    if ".." in name:
        raise StoreError(f"invalid chroma collection name {name!r}: must not contain '..'")
    if _IPV4_RE.match(name):
        raise StoreError(f"invalid chroma collection name {name!r}: must not be an IPv4 address")
    return name


def _physical_name(prefix: str, name: str) -> str:
    """Map a logical collection name to its physical Chroma name (Decision 4)."""
    check_collection_name(name)
    return _check_chroma_name(prefix + name)


def _auth_headers(token_header: str, token: str | None) -> dict[str, str] | None:
    """Build the auth header for a Chroma HTTP server or proxy.

    ``Authorization`` carries ``Bearer <token>`` (Chroma's own convention); any other header name
    (e.g. ``x-chroma-token``, used by Chroma Cloud and common proxies) carries the raw token.
    """
    if not token:
        return None
    header = token_header.strip()
    value = f"Bearer {token}" if header.lower() == "authorization" else token
    return {header: value}


def translate_filter(f: Filter, fields: dict[str, FieldType]) -> dict[str, Any]:
    if isinstance(f, (And, Or)):
        key = "$and" if isinstance(f, And) else "$or"
        return {key: [translate_filter(item, fields) for item in f.items]}
    assert isinstance(f, Cond)
    ftype = fields.get(f.field, "str")
    if isinstance(f.value, tuple):
        return {f.field: {f.op: [_value(v, ftype) for v in f.value]}}
    return {f.field: {f.op: _value(f.value, ftype)}}


class ChromaStore:
    # The core batches upserts by this; each upsert is split further if the client reports a
    # smaller maximum batch size. The class value is the type's best case; each instance sets its
    # own (Chroma Cloud caps writes at 300 records).
    capabilities: Capabilities = Capabilities(metrics=frozenset({"cosine", "l2", "dot"}), max_batch=DEFAULT_MAX_BATCH)

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = "",
        max_batch: int | None = None,
        hnsw_max_neighbors: int | None = None,
        hnsw_ef_construction: int | None = None,
        ef_search: int | None = None,
    ) -> None:
        self._chromadb = _import()
        self._client = client
        self._prefix = prefix
        self._hnsw_max_neighbors = hnsw_max_neighbors
        self._hnsw_ef_construction = hnsw_ef_construction
        self._ef_search = ef_search
        if max_batch is None:
            try:
                reported = int(client.get_max_batch_size())
            except Exception:
                reported = DEFAULT_MAX_BATCH
            max_batch = max(1, min(reported, DEFAULT_MAX_BATCH))
        self.client_max_batch = max_batch
        self.capabilities = Capabilities(metrics=frozenset({"cosine", "l2", "dot"}), max_batch=max_batch)
        self._lock = threading.RLock()

    @classmethod
    def ephemeral(
        cls,
        *,
        tenant: str | None = None,
        database: str | None = None,
        prefix: str = "",
        hnsw_max_neighbors: int | None = None,
        hnsw_ef_construction: int | None = None,
        ef_search: int | None = None,
    ) -> ChromaStore:
        chromadb = _import()
        tenant, database = _local_tenant_database(chromadb, tenant, database)
        _ensure_tenant_database(chromadb, _client_settings(chromadb, allow_reset=True), tenant, database)
        client = chromadb.EphemeralClient(
            settings=_client_settings(chromadb, allow_reset=True), tenant=tenant, database=database
        )
        return cls(
            client,
            prefix=prefix,
            hnsw_max_neighbors=hnsw_max_neighbors,
            hnsw_ef_construction=hnsw_ef_construction,
            ef_search=ef_search,
        )

    @classmethod
    def persistent(
        cls,
        path: str | Path,
        *,
        tenant: str | None = None,
        database: str | None = None,
        prefix: str = "",
        hnsw_max_neighbors: int | None = None,
        hnsw_ef_construction: int | None = None,
        ef_search: int | None = None,
    ) -> ChromaStore:
        chromadb = _import()
        tenant, database = _local_tenant_database(chromadb, tenant, database)
        admin_settings = _client_settings(chromadb, is_persistent=True, persist_directory=str(path))
        _ensure_tenant_database(chromadb, admin_settings, tenant, database)
        client = chromadb.PersistentClient(path=str(path), settings=_client_settings(chromadb),
                                           tenant=tenant, database=database)  # fmt: skip
        return cls(
            client,
            prefix=prefix,
            hnsw_max_neighbors=hnsw_max_neighbors,
            hnsw_ef_construction=hnsw_ef_construction,
            ef_search=ef_search,
        )

    @classmethod
    def http(
        cls,
        host: str,
        port: int = 8000,
        ssl: bool = False,
        headers: dict[str, str] | None = None,
        *,
        tenant: str | None = None,
        database: str | None = None,
        prefix: str = "",
        hnsw_max_neighbors: int | None = None,
        hnsw_ef_construction: int | None = None,
        ef_search: int | None = None,
    ) -> ChromaStore:
        chromadb = _import()
        tenant, database = _local_tenant_database(chromadb, tenant, database)
        try:
            client = chromadb.HttpClient(host=host, port=port, ssl=ssl, headers=headers,
                                         settings=_client_settings(chromadb), tenant=tenant, database=database)  # fmt: skip
        except Exception as exc:
            raise StoreError(
                f"chroma: could not connect to {host}:{port} (tenant={tenant!r}, database={database!r}); "
                f"a tenant or database that does not exist on the server must be created first: {exc}"
            ) from exc
        return cls(
            client,
            prefix=prefix,
            hnsw_max_neighbors=hnsw_max_neighbors,
            hnsw_ef_construction=hnsw_ef_construction,
            ef_search=ef_search,
        )

    @classmethod
    def cloud(
        cls,
        api_key: str,
        *,
        tenant: str | None = None,
        database: str | None = None,
        cloud_host: str = DEFAULT_CLOUD_HOST,
        prefix: str = "",
        hnsw_max_neighbors: int | None = None,
        hnsw_ef_construction: int | None = None,
        ef_search: int | None = None,
    ) -> ChromaStore:
        chromadb = _import()
        try:
            client = chromadb.CloudClient(
                tenant=tenant,
                database=database,
                api_key=api_key,
                settings=_client_settings(chromadb),
                cloud_host=cloud_host,
            )
        except Exception as exc:
            raise StoreError(
                f"chroma: could not connect to Chroma Cloud at {cloud_host} "
                f"(tenant={tenant!r}, database={database!r}): {exc}"
            ) from exc
        return cls(
            client,
            prefix=prefix,
            max_batch=CLOUD_MAX_BATCH,
            hnsw_max_neighbors=hnsw_max_neighbors,
            hnsw_ef_construction=hnsw_ef_construction,
            ef_search=ef_search,
        )

    # ---- helpers ------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _errors(self, name: str, what: str) -> Iterator[None]:
        try:
            yield
        except (StoreError, CollectionMismatch):
            raise
        except self._chromadb.errors.NotFoundError as exc:
            raise CollectionNotFound(f"collection {name} does not exist") from exc
        except Exception as exc:
            raise StoreError(f"chroma: {what} on {name} failed: {exc}") from exc

    def _physical(self, name: str) -> str:
        return _physical_name(self._prefix, name)

    def _get(self, name: str) -> Any | None:
        physical = self._physical(name)
        with self._errors(name, "get collection"):
            try:
                return self._client.get_collection(physical, embedding_function=None)
            except self._chromadb.errors.NotFoundError:
                return None

    def _require(self, name: str) -> tuple[Any, int, Metric, dict[str, FieldType]]:
        col = self._get(name)
        if col is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        dim, metric, fields = self._describe(col)
        return col, dim, metric, fields

    @staticmethod
    def _describe(col: Any) -> tuple[int, Metric, dict[str, FieldType]]:
        meta = col.metadata or {}
        if "rc_dim" not in meta:
            raise StoreError(f"chroma collection {col.name} was not created by RAGCentre")
        space = ((col.configuration or {}).get("hnsw") or {}).get("space") or "l2"
        metric = next(m for m, s in _SPACES.items() if s == space)
        return int(meta["rc_dim"]), metric, json.loads(meta.get("rc_fields") or "{}")

    @staticmethod
    def _immutable_options(col: Any) -> tuple[int | None, int | None]:
        """The HNSW options a collection was built with, as recorded in its metadata."""
        meta = col.metadata or {}

        def read(key: str) -> int | None:
            value = meta.get(key)
            return None if value is None else int(value)

        return read("rc_hnsw_max_neighbors"), read("rc_hnsw_ef_construction")

    def _hnsw_config(self, space: str) -> dict[str, Any]:
        config: dict[str, Any] = {"space": space}
        if self._hnsw_max_neighbors is not None:
            config["max_neighbors"] = self._hnsw_max_neighbors
        if self._hnsw_ef_construction is not None:
            config["ef_construction"] = self._hnsw_ef_construction
        if self._ef_search is not None:
            config["ef_search"] = self._ef_search
        return config

    def _collection_metadata(self, spec: CollectionSpec) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "rc_dim": spec.dim,
            "rc_fields": json.dumps(dict(spec.fields), sort_keys=True),
        }
        # Chroma's Rust bindings reject None in collection metadata, so record an option only
        # when it was explicitly set; an absent key reads back as None.
        if self._hnsw_max_neighbors is not None:
            meta["rc_hnsw_max_neighbors"] = self._hnsw_max_neighbors
        if self._hnsw_ef_construction is not None:
            meta["rc_hnsw_ef_construction"] = self._hnsw_ef_construction
        return meta

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        try:
            self._client.heartbeat()
        except Exception as exc:
            raise StoreError(f"chroma is not reachable: {exc}") from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        physical = self._physical(spec.name)
        with self._lock, self._errors(spec.name, "ensure collection"):
            col = self._client.get_or_create_collection(
                physical,
                configuration={"hnsw": self._hnsw_config(_SPACES[spec.metric])},
                metadata=self._collection_metadata(spec),
                embedding_function=None,
            )
            dim, metric, fields = self._describe(col)
            if dim != spec.dim or metric != spec.metric:
                raise CollectionMismatch(
                    f"collection {spec.name} exists with dim={dim}, metric={metric}; "
                    f"requested dim={spec.dim}, metric={spec.metric}"
                )
            want_neighbors = self._hnsw_max_neighbors
            want_construction = self._hnsw_ef_construction
            got_neighbors, got_construction = self._immutable_options(col)
            if got_neighbors != want_neighbors or got_construction != want_construction:
                raise CollectionMismatch(
                    f"collection {spec.name} was built with hnsw_max_neighbors={got_neighbors}, "
                    f"hnsw_ef_construction={got_construction}; requested "
                    f"hnsw_max_neighbors={want_neighbors}, hnsw_ef_construction={want_construction}"
                )
            for fname, ftype in spec.fields.items():
                if fname in fields and fields[fname] != ftype:
                    raise CollectionMismatch(f"field {fname} is {fields[fname]} in {spec.name}, not {ftype}")
            merged = {**fields, **spec.fields}
            if merged != fields:
                col.modify(metadata={**(col.metadata or {}), "rc_fields": json.dumps(merged, sort_keys=True)})
            # ef_search is query-time mutable: apply it when it differs from the live collection.
            if self._ef_search is not None:
                current = ((col.configuration or {}).get("hnsw") or {}).get("ef_search")
                if current != self._ef_search:
                    col.modify(configuration={"hnsw": {"ef_search": self._ef_search}})

    def collection_info(self, name: str) -> CollectionInfo | None:
        col = self._get(name)
        if col is None:
            return None
        dim, metric, _ = self._describe(col)
        with self._errors(name, "count"):
            count = int(col.count())
        return CollectionInfo(dim=dim, metric=metric, count=count)

    def drop_collection(self, name: str) -> None:
        physical = self._physical(name)
        with (
            self._lock,
            self._errors(name, "drop collection"),
            contextlib.suppress(self._chromadb.errors.NotFoundError),
        ):
            self._client.delete_collection(physical)

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        if not records:
            return
        col, dim, _, fields = self._require(name)
        for rec in records:
            if len(rec.vector) != dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {dim}")
        for start in range(0, len(records), self.client_max_batch):
            part = records[start : start + self.client_max_batch]
            with self._errors(name, "upsert"):
                col.upsert(
                    ids=[r.id for r in part],
                    embeddings=[list(map(float, r.vector)) for r in part],
                    # Chroma rejects an empty metadata dict; None means "no metadata".
                    metadatas=[
                        {k: _value(v, fields.get(k, "str")) for k, v in r.metadata.items()} or None for r in part
                    ],
                )

    def delete(self, name: str, ids: Sequence[str]) -> None:
        if not ids:
            return
        col = self._get(name)
        if col is None:
            return
        with contextlib.suppress(CollectionNotFound), self._errors(name, "delete"):
            col.delete(ids=list(ids))

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        col, dim, metric, fields = self._require(name)
        if len(vector) != dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {dim}")
        if k < 1:
            return []
        where = None if filter is None else translate_filter(filter, {**fields, "document_id": "str"})
        with self._errors(name, "query"):
            res = col.query(query_embeddings=[list(map(float, vector))], n_results=k, where=where,
                            include=["distances"])  # fmt: skip
        ids = (res.get("ids") or [[]])[0]
        distances = (res.get("distances") or [[]])[0]
        return [Match(id=i, score=_score(metric, float(d))) for i, d in zip(ids, distances, strict=True)]

    def close(self) -> None:
        pass  # Chroma clients hold no resources that need explicit release


def _local_tenant_database(chromadb: Any, tenant: str | None, database: str | None) -> tuple[str, str]:
    # The local/http factories coerce None to "None" (str(None)), so resolve the defaults here.
    if tenant is None:
        tenant = chromadb.config.DEFAULT_TENANT
    if database is None:
        database = chromadb.config.DEFAULT_DATABASE
    return tenant, database


def _ensure_tenant_database(chromadb: Any, settings: Any, tenant: str, database: str) -> None:
    """Create the tenant and database in an embedded Chroma when they do not exist yet.

    A server (``http`` mode) or Chroma Cloud owns its tenants, so those modes never create them;
    an embedded directory belongs to RAGCentre, and Chroma refuses to open a missing tenant.
    """
    if tenant == chromadb.config.DEFAULT_TENANT and database == chromadb.config.DEFAULT_DATABASE:
        return
    errors = chromadb.errors
    admin = chromadb.AdminClient(settings)
    try:
        admin.get_tenant(tenant)
    except errors.NotFoundError:
        admin.create_tenant(tenant)
    try:
        admin.get_database(database, tenant)
    except errors.NotFoundError:
        admin.create_database(database, tenant)


# ---- registration ---------------------------------------------------------------------------


class ChromaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["local", "http", "cloud"] = Field(
        default="local", description="Deployment mode: local directory, HTTP server, or Chroma Cloud"
    )
    host: str | None = Field(default=None, description="Chroma server host (mode=http)")
    port: int = Field(default=8000, ge=1, le=65535, description="Chroma server port (mode=http)")
    ssl: bool = Field(default=False, description="Use HTTPS for the Chroma server (mode=http)")
    path: str | None = Field(
        default=None, description="Directory for a persistent local client (mode=local; default <data dir>/chroma)"
    )
    cloud_host: str = Field(default=DEFAULT_CLOUD_HOST, description="Chroma Cloud host (mode=cloud)")
    tenant: str | None = Field(default=None, description="Chroma tenant (default 'default_tenant')")
    database: str | None = Field(default=None, description="Chroma database (default 'default_database')")
    token_header: str = Field(
        default="Authorization",
        description="HTTP auth header name; 'Authorization' sends 'Bearer <token>', others send the raw token",
    )
    prefix: str = Field(default="", description="Prefix prepended to every collection name")
    hnsw_max_neighbors: int | None = Field(default=None, ge=1, description="HNSW M (immutable)")
    hnsw_ef_construction: int | None = Field(default=None, ge=1, description="HNSW ef_construction (immutable)")
    ef_search: int | None = Field(default=None, ge=1, description="HNSW ef_search (mutable, query time)")

    @field_validator("token_header")
    @classmethod
    def _token_header_not_empty(cls, v: str) -> str:
        header = v.strip()
        if not header:
            raise ValueError("token_header must not be empty")
        return header

    @field_validator("prefix")
    @classmethod
    def _valid_prefix(cls, v: str) -> str:
        if v and (re.fullmatch(r"[a-zA-Z0-9._-]*", v) is None or ".." in v):
            raise ValueError("prefix must contain only [a-zA-Z0-9._-] and no '..'")
        return v

    @model_validator(mode="before")
    @classmethod
    def _infer_mode(cls, data: Any) -> Any:
        # Providers created before `mode` existed used host/port/ssl (http) or path (local).
        if isinstance(data, dict) and "mode" not in data:
            data = dict(data)
            if data.get("host") is not None:
                data["mode"] = "http"
            elif data.get("path") is not None:
                data["mode"] = "local"
        return data

    @model_validator(mode="after")
    def _mode_consistency(self) -> ChromaConfig:
        if self.mode == "cloud":
            if self.host is not None or self.path is not None:
                raise ValueError("mode='cloud' does not take host or path")
        elif self.mode == "http":
            if self.host is None:
                raise ValueError("mode='http' requires host")
            if self.path is not None:
                raise ValueError("mode='http' does not take path")
        elif self.host is not None:
            raise ValueError("set mode='http' to use host")
        return self


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> ChromaStore:
    assert isinstance(config, ChromaConfig)
    prefix = config.prefix
    if config.mode == "cloud":
        api_key = secrets.get("api_key")
        if not api_key:
            raise StoreError("chroma mode='cloud' requires the 'api_key' secret")
        return ChromaStore.cloud(
            api_key,
            tenant=config.tenant,
            database=config.database,
            cloud_host=config.cloud_host,
            prefix=prefix,
            hnsw_max_neighbors=config.hnsw_max_neighbors,
            hnsw_ef_construction=config.hnsw_ef_construction,
            ef_search=config.ef_search,
        )
    if config.mode == "http":
        assert config.host is not None  # guaranteed by ChromaConfig
        headers = _auth_headers(config.token_header, secrets.get("token"))
        return ChromaStore.http(
            config.host,
            config.port,
            config.ssl,
            headers,
            tenant=config.tenant,
            database=config.database,
            prefix=prefix,
            hnsw_max_neighbors=config.hnsw_max_neighbors,
            hnsw_ef_construction=config.hnsw_ef_construction,
            ef_search=config.ef_search,
        )
    path = Path(config.path) if config.path else data_dir / "chroma"
    if not path.is_absolute():
        path = data_dir / path
    return ChromaStore.persistent(
        path,
        tenant=config.tenant,
        database=config.database,
        prefix=prefix,
        hnsw_max_neighbors=config.hnsw_max_neighbors,
        hnsw_ef_construction=config.hnsw_ef_construction,
        ef_search=config.ef_search,
    )


STORE_TYPE = VectorStoreType(
    type="chroma",
    label="Chroma",
    description="Chroma server, Chroma Cloud, or a persistent local Chroma directory. Needs the optional 'chroma' extra.",
    config_model=ChromaConfig,
    secret_keys=("token", "api_key"),
    capabilities=ChromaStore.capabilities,
    factory=_factory,
    immutable_fields=("hnsw_max_neighbors", "hnsw_ef_construction"),
)
