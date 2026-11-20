"""Milvus vector store (optional extra ``ragcentre[milvus]``, or ``ragcentre[milvus-lite]``).

Talks to a Milvus server (``http://host:19530``, Zilliz Cloud) or, with the ``milvus-lite`` extra,
to Milvus Lite through a local path ending in ``.db`` (Lite keeps it as a directory). Each RAGCentre collection is one Milvus collection
named ``<collection_prefix><name>`` with three fields: a VARCHAR primary key ``id``, a FLOAT_VECTOR
``vector`` and a JSON field ``meta`` holding the record's flat metadata. Collections use the
``Strong`` consistency level, so a write is visible to the next query.

Milvus Lite has no collection description or field-addition API, so field types and index options
are kept in a ``LocalCatalog`` (``<data_dir>/store_catalogs/milvus.db``). The physical dimension,
metric and index type are read back from Milvus and checked against it on every
``ensure_collection``.

``pymilvus`` is imported only when a store is created, so ``import ragcentre`` works without the
extra.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import And, Cond, Filter, Or
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

IndexType = Literal["AUTOINDEX", "FLAT", "HNSW"]
_METRICS: dict[Metric, str] = {"cosine": "COSINE", "l2": "L2", "dot": "IP"}
_PHYSICAL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")
_PREFIX_RE = re.compile(r"^[a-z0-9_]{0,32}$")
_NOT_FOUND = 100  # Milvus error code for a missing collection
MAX_TOP_K = 16384  # Milvus's limit on topk
MAX_BATCH = 256


def _import() -> Any:
    return require_extra("pymilvus", store="milvus", extra="milvus", package="pymilvus")


def _is_not_found(exc: Exception) -> bool:
    if getattr(exc, "code", None) == _NOT_FOUND:
        return True
    text = str(exc).lower()
    return "not exist" in text or "not found" in text


# ---- filter translation ---------------------------------------------------------------------


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(float(value)) if isinstance(value, float) else str(value)
    return json.dumps(str(value))  # a double-quoted string with JSON escapes, which Milvus accepts


def _path(name: str) -> str:
    return f"meta[{json.dumps(name)}]"


def translate_filter(f: Filter) -> str:
    """Translate a parsed filter to a Milvus boolean expression over the JSON ``meta`` field.

    Field names are validated by the corpus schema (``FIELD_RE``) and quoted; values are written as
    literals. ``in`` is not supported on JSON paths in Milvus Lite, so ``$in`` / ``$nin`` expand to
    ``==`` / ``!=`` chains.
    """
    if isinstance(f, And):
        return "(" + " and ".join(translate_filter(item) for item in f.items) + ")"
    if isinstance(f, Or):
        return "(" + " or ".join(translate_filter(item) for item in f.items) + ")"
    return _condition(f)


def _condition(c: Cond) -> str:
    path = _path(c.field)
    if c.op in ("$in", "$nin"):
        assert isinstance(c.value, tuple)
        joiner, op = (" or ", "==") if c.op == "$in" else (" and ", "!=")
        return "(" + joiner.join(f"{path} {op} {_literal(v)}" for v in c.value) + ")"
    op = {"$eq": "==", "$ne": "!=", "$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}[c.op]
    return f"({path} {op} {_literal(c.value)})"


# ---- store ----------------------------------------------------------------------------------


class MilvusStore:
    def __init__(
        self,
        client: Any,
        catalog: LocalCatalog,
        *,
        index_type: IndexType = "AUTOINDEX",
        collection_prefix: str = "",
        owns_catalog: bool = True,
    ) -> None:
        self._client = client
        self._catalog = catalog
        self._owns_catalog = owns_catalog
        self._index_type = index_type
        self._prefix = collection_prefix
        self._loaded: set[str] = set()
        self._lock = threading.Lock()  # guards _loaded and collection creation
        # FLAT is brute force; the graph indexes are approximate, including under a filter.
        self.capabilities = Capabilities(
            metrics=frozenset(_METRICS), max_batch=MAX_BATCH, max_dim=32768, filter_exact=index_type == "FLAT"
        )

    @classmethod
    def connect(
        cls,
        *,
        uri: str,
        data_dir: Path,
        token: str | None = None,
        db_name: str | None = None,
        index_type: IndexType = "AUTOINDEX",
        collection_prefix: str = "",
    ) -> MilvusStore:
        pymilvus = _import()
        kwargs: dict[str, Any] = {"uri": uri}
        if token:
            kwargs["token"] = token
        if db_name:
            kwargs["db_name"] = db_name
        try:
            client = pymilvus.MilvusClient(**kwargs)
        except Exception as exc:
            raise StoreError(f"milvus: could not connect to {uri}: {exc}") from exc
        # Credentials never go into the key; rotating a token keeps the same catalog entries.
        key = f"{uri}/{db_name or 'default'}/{collection_prefix}"
        return cls(client, LocalCatalog(data_dir, "milvus", key), index_type=index_type,
                   collection_prefix=collection_prefix)  # fmt: skip

    # ---- helpers ------------------------------------------------------------------------------

    def _physical(self, name: str) -> str:
        check_collection_name(name)
        physical = self._prefix + name
        if not _PHYSICAL_RE.match(physical):
            raise StoreError(f"milvus: invalid collection name {physical!r}")
        return physical

    def _options(self) -> dict[str, Any]:
        return {"index_type": self._index_type}

    def _wrap(self, name: str, what: str, exc: Exception) -> StoreError:
        if _is_not_found(exc):
            self._forget(name)
            return CollectionNotFound(f"collection {name} does not exist")
        return StoreError(f"milvus: {what} on {name} failed: {exc}")

    def _forget(self, name: str) -> None:
        with self._lock:
            self._loaded.discard(name)

    def _entry(self, name: str) -> CatalogEntry:
        """The catalog entry of a collection that must exist, loaded into memory for search."""
        physical = self._physical(name)
        entry = self._catalog.get(name)
        if entry is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        with self._lock:
            if name in self._loaded:
                return entry
        try:
            self._client.load_collection(physical)  # a reconnected client sees collections released
        except Exception as exc:
            raise self._wrap(name, "load", exc) from exc
        with self._lock:
            self._loaded.add(name)
        return entry

    def _physical_shape(self, physical: str) -> tuple[int, Metric, str] | None:
        """(dim, metric, index type) of an existing collection, or None if there is none."""
        try:
            desc = self._client.describe_collection(physical)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise StoreError(f"milvus: could not describe {physical}: {exc}") from exc
        fields = {f["name"]: f for f in desc.get("fields", [])}
        if set(fields) != {"id", "vector", "meta"}:
            raise StoreError(f"milvus collection {physical} was not created by RAGCentre (fields {sorted(fields)})")
        dim = int(fields["vector"].get("params", {}).get("dim", 0))
        try:
            index = self._client.describe_index(physical, "vector")
        except Exception as exc:
            raise StoreError(f"milvus: could not describe the index of {physical}: {exc}") from exc
        metric = next((m for m, v in _METRICS.items() if v == index.get("metric_type")), None)
        if metric is None:
            raise StoreError(f"milvus collection {physical} uses unsupported metric {index.get('metric_type')!r}")
        return dim, metric, str(index.get("index_type"))

    def _create(self, physical: str, entry: CatalogEntry) -> None:
        pymilvus = _import()
        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", pymilvus.DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("vector", pymilvus.DataType.FLOAT_VECTOR, dim=entry.dim)
        schema.add_field("meta", pymilvus.DataType.JSON)
        index = self._client.prepare_index_params()
        params: dict[str, Any] = {"M": 16, "efConstruction": 200} if self._index_type == "HNSW" else {}
        index.add_index("vector", index_type=self._index_type, metric_type=_METRICS[entry.metric], params=params)
        self._client.create_collection(physical, schema=schema, index_params=index, consistency_level="Strong")

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        try:
            self._client.list_collections()
        except Exception as exc:
            raise StoreError(f"milvus is not reachable: {exc}") from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        physical = self._physical(spec.name)
        # Reject uncatalogued remote collections rather than adopting unknown field types.
        shape = self._physical_shape(physical)
        if shape is not None and self._catalog.get(spec.name) is None:
            raise StoreError(f"milvus: collection {spec.name} has no local catalog; restore its catalog or rebuild")
        # A changed dimension, metric, field type or index option is rejected before modifying
        # an existing physical collection. A pending entry permits retry after creation fails.
        entry = self._catalog.ensure(spec, self._options())
        if shape is None:
            with self._lock:
                shape = self._physical_shape(physical)  # created by another thread meanwhile?
                if shape is None:
                    try:
                        self._create(physical, entry)
                    except Exception as exc:
                        shape = self._physical_shape(physical)  # created by another process?
                        if shape is None:
                            raise StoreError(f"milvus: could not create collection {spec.name}: {exc}") from exc
                    self._loaded.discard(spec.name)
            shape = shape or self._physical_shape(physical)
        if shape is None:
            raise StoreError(f"milvus: collection {spec.name} vanished while it was being created")
        dim, metric, index_type = shape
        if (dim, metric) != (entry.dim, entry.metric):
            raise CollectionMismatch(
                f"milvus collection {physical} has dim={dim}, metric={metric}; "
                f"the catalog records dim={entry.dim}, metric={entry.metric}"
            )
        # Milvus Lite reports AUTOINDEX as the index it actually built; only check explicit choices.
        if self._index_type != "AUTOINDEX" and index_type not in (self._index_type, "AUTOINDEX"):
            raise CollectionMismatch(f"milvus collection {physical} has index {index_type}, not {self._index_type}")
        self._entry(spec.name)

    def collection_info(self, name: str) -> CollectionInfo | None:
        physical = self._physical(name)
        entry = self._catalog.get(name)
        if entry is None:
            return None
        shape = self._physical_shape(physical)
        if shape is None:
            return None
        self._entry(name)
        try:
            rows = self._client.query(physical, filter="", output_fields=["count(*)"], consistency_level="Strong")
        except Exception as exc:
            raise self._wrap(name, "count", exc) from exc
        return CollectionInfo(dim=shape[0], metric=shape[1], count=int(rows[0]["count(*)"]))

    def drop_collection(self, name: str) -> None:
        physical = self._physical(name)
        self._forget(name)
        try:
            self._client.drop_collection(physical)  # a missing collection is not an error in Milvus
        except Exception as exc:
            if not _is_not_found(exc):
                raise StoreError(f"milvus: could not drop collection {name}: {exc}") from exc
        self._catalog.drop(name)

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        entry = self._entry(name)
        rows = []
        for rec in records:
            if len(rec.vector) != entry.dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {entry.dim}")
            meta = {k: float(v) if entry.fields.get(k) == "float" else v for k, v in rec.metadata.items()}
            rows.append({"id": rec.id, "vector": [float(x) for x in rec.vector], "meta": meta})
        if not rows:
            return
        try:
            self._client.upsert(self._physical(name), rows)
        except Exception as exc:
            raise self._wrap(name, "upsert", exc) from exc

    def delete(self, name: str, ids: Sequence[str]) -> None:
        physical = self._physical(name)
        if not ids:
            return
        try:
            self._entry(name)
        except CollectionNotFound:
            return
        try:
            self._client.delete(physical, ids=list(ids))
        except Exception as exc:
            if not _is_not_found(exc):
                raise StoreError(f"milvus: delete on {name} failed: {exc}") from exc

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        entry = self._entry(name)
        if len(vector) != entry.dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {entry.dim}")
        if k < 1:
            return []
        kwargs: dict[str, Any] = {
            "limit": min(k, MAX_TOP_K),
            "output_fields": [],
            "search_params": {"metric_type": _METRICS[entry.metric]},
            "consistency_level": "Strong",
        }
        if filter is not None:
            kwargs["filter"] = translate_filter(filter)
        try:
            hits = self._client.search(self._physical(name), [[float(x) for x in vector]], **kwargs)[0]
        except Exception as exc:
            raise self._wrap(name, "query", exc) from exc
        if entry.metric == "l2":  # Milvus reports squared Euclidean distance
            return [Match(id=str(h["id"]), score=-math.sqrt(max(float(h["distance"]), 0.0))) for h in hits]
        return [Match(id=str(h["id"]), score=float(h["distance"])) for h in hits]

    def close(self) -> None:
        with suppress(Exception):  # still close the catalog if client teardown fails
            self._client.close()
        if self._owns_catalog:
            self._catalog.close()


# ---- registration ---------------------------------------------------------------------------


class MilvusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uri: str = Field(
        default="milvus.db",
        description=(
            "Milvus server or Zilliz Cloud URI (e.g. http://localhost:19530), or a local Milvus Lite path "
            "ending in .db (relative paths are under the data directory; needs the 'milvus-lite' extra)"
        ),
    )
    db_name: str | None = Field(default=None, description="Milvus database; the server's default when empty")
    collection_prefix: str = Field(
        default="", description="Prepended to every collection name, to share one Milvus database"
    )
    index_type: IndexType = Field(
        default="AUTOINDEX", description="Vector index: AUTOINDEX, HNSW, or FLAT (exact brute force)"
    )

    @field_validator("collection_prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        if not _PREFIX_RE.match(value):
            raise ValueError("use at most 32 lowercase letters, digits and underscores")
        return value

    @field_validator("uri")
    @classmethod
    def _uri(cls, value: str) -> str:
        if not _is_remote(value) and not value.endswith(".db"):
            raise ValueError("a Milvus Lite path must end in .db; a server URI starts with http:// or https://")
        return value


def _is_remote(uri: str) -> bool:
    return "://" in uri


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> MilvusStore:
    assert isinstance(config, MilvusConfig)
    uri = config.uri
    if not _is_remote(uri) and not Path(uri).is_absolute():
        uri = str(data_dir / uri)
    return MilvusStore.connect(uri=uri, data_dir=data_dir, token=secrets.get("token") or None,
                               db_name=config.db_name, index_type=config.index_type,
                               collection_prefix=config.collection_prefix)  # fmt: skip


STORE_TYPE = VectorStoreType(
    type="milvus",
    label="Milvus",
    description=(
        "Milvus server, Zilliz Cloud, or a local Milvus Lite database. "
        "Needs the optional 'milvus' extra ('milvus-lite' for local directories)."
    ),
    config_model=MilvusConfig,
    secret_keys=("token",),
    capabilities=Capabilities(metrics=frozenset(_METRICS), max_batch=MAX_BATCH, max_dim=32768),
    factory=_factory,
    immutable_fields=("index_type",),
)
