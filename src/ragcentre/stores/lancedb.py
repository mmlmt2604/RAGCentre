"""LanceDB vector store (optional extra ``ragcentre[lancedb]``).

One collection is one LanceDB table named ``<prefix><collection>``. Columns: ``_rc_id`` (string
primary key), ``_rc_vector`` (``float32`` fixed-size list, so the dimension is the schema) and one
typed column per filterable field plus ``document_id`` (string, int64, float64 and bool).

**Catalog.** The collection's metric and index options are written once into the Arrow *schema
metadata* (key ``ragcentre``) when the table is created. LanceDB 0.41 has no API to change
schema-level metadata afterwards (only per-field metadata), but nothing here needs to change:
dimension and filter fields are read from the columns themselves, and a field added later is an
``add_columns`` call that leaves the schema metadata untouched. No side table or ``LocalCatalog``
is needed, and there is no catalog write that could race.

**Search.** Vector search with ``prefilter=True`` (the filter narrows the rows *before* ranking,
so ``k`` matches come back when ``k`` exist). With no vector index the search is an exact scan.
When ``ann_index_min_rows`` is set and a collection reaches that many rows, an ANN index
(``index_type``) is built after the upsert that crossed the threshold; rows written later are
scanned flat until the index is refreshed. If an indexed, filtered query still returns fewer than
``k`` rows, it is re-run without the index (an exact scan of the filtered rows), so
``filter_exact`` holds on local and object-store tables. The ``db://`` cloud service is only
read about, not run, so it declares ``filter_exact=False`` and ``consistency="eventual"``.

**Scores.** ``_distance`` is ``1 - cos`` for cosine, ``1 - dot`` for dot, and the *squared*
Euclidean distance for l2 (all three verified against lancedb 0.41.0), which become ``1 - d``,
``1 - d`` and ``-sqrt(d)``. A zero vector has no cosine distance: LanceDB leaves such rows out of
cosine results, so they cannot be retrieved (a zero embedding carries no signal anyway).

``lancedb`` and ``pyarrow`` are imported only when a store is created, so ``import ragcentre``
works without the extra.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import FieldType, Filter
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
from ragcentre.stores.catalog import CatalogEntry, reconcile
from ragcentre.stores.filter_datafusion import compile_datafusion, string_literal
from ragcentre.stores.registry import VectorStoreType

log = logging.getLogger("ragcentre.stores.lancedb")

META_KEY = b"ragcentre"
ID_COL = "_rc_id"
VECTOR_COL = "_rc_vector"
DISTANCE_COL = "_distance"
INDEX_NAME = "_rc_vector_idx"
ID_INDEX_NAME = "_rc_id_idx"
RESERVED_PREFIX = "_rc_"
MIN_ANN_ROWS = 256  # the IVF indexes train on a sample of the rows; fewer than this is not worth it
OPTIMIZE_EVERY = 64  # compact the many small fragments that repeated upserts create

IndexType = Literal["IVF_FLAT", "IVF_SQ", "IVF_PQ", "IVF_HNSW_SQ"]
_INDEX_CLASS: dict[str, str] = {
    "IVF_FLAT": "IvfFlat",
    "IVF_SQ": "IvfSq",
    "IVF_PQ": "IvfPq",
    "IVF_HNSW_SQ": "HnswSq",
}
_TABLE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.-]*$")

# What a LanceDB local or object-store table delivers; ``VectorStoreType.capabilities`` shows it.
CAPABILITIES = Capabilities(metrics=frozenset({"cosine", "l2", "dot"}), max_batch=1000)


def _import() -> tuple[Any, Any, Any]:
    lancedb = require_extra("lancedb", store="lancedb", extra="lancedb", package="lancedb")
    pa = require_extra("pyarrow", store="lancedb", extra="lancedb", package="pyarrow")
    index = require_extra("lancedb.index", store="lancedb", extra="lancedb", package="lancedb")
    return lancedb, pa, index


def _is_missing(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    text = str(exc).lower()
    return "not found" in text or "does not exist" in text


def score_from_distance(metric: Metric, distance: float) -> float:
    """RAGCentre's score for a LanceDB ``_distance`` (plan section 4.1)."""
    if metric == "l2":  # LanceDB returns the squared Euclidean distance
        return -math.sqrt(max(distance, 0.0))
    return 1.0 - distance  # cosine: d = 1 - cos; dot: d = 1 - dot


class LanceDBStore:
    def __init__(
        self,
        db: Any,
        *,
        remote: bool = False,
        local_path: str | None = None,
        prefix: str = "",
        ann_index_min_rows: int | None = 50_000,
        index_type: IndexType = "IVF_FLAT",
        nprobes: int | None = None,
    ) -> None:
        _, self._pa, self._index = _import()
        self._db = db
        self._remote = remote
        self._local_path = local_path
        self._prefix = prefix
        self._ann_min_rows = ann_index_min_rows
        self._index_type = index_type
        self._nprobes = nprobes
        # Remote (db://) tables are not exercised here: stay conservative on what they guarantee.
        self.capabilities = replace(
            CAPABILITIES,
            filter_exact=not remote,
            consistency="eventual" if remote else "immediate",
        )
        self._ensure_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._upserts: dict[str, int] = {}

    @classmethod
    def connect(
        cls,
        uri: str,
        *,
        api_key: str | None = None,
        region: str | None = None,
        prefix: str = "",
        ann_index_min_rows: int | None = 50_000,
        index_type: IndexType = "IVF_FLAT",
        nprobes: int | None = None,
    ) -> LanceDBStore:
        lancedb, _, _ = _import()
        remote = uri.startswith("db://")
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if region:
            if remote:
                kwargs["region"] = region
            else:  # object storage: the region goes to the storage layer (read in the docs, not run)
                kwargs["storage_options"] = {"aws_region": region}
        try:
            db = lancedb.connect(uri, **kwargs)
        except Exception as exc:
            raise StoreError(f"lancedb: could not open {_safe_uri(uri)}: {exc}") from exc
        return cls(
            db,
            remote=remote,
            local_path=None if "://" in uri else uri,
            prefix=prefix,
            ann_index_min_rows=ann_index_min_rows,
            index_type=index_type,
            nprobes=nprobes,
        )

    # ---- helpers ------------------------------------------------------------------------------

    def _physical(self, name: str) -> str:
        check_collection_name(name)
        physical = self._prefix + name
        if not _TABLE_RE.match(physical):
            raise StoreError(
                f"collection name {physical!r} (prefix {self._prefix!r} plus {name!r}) is not a valid LanceDB table name"
            )
        return physical

    def _options(self) -> dict[str, Any]:
        """The index-shaping options, recorded in the schema metadata and compared on every ensure."""
        return {"index_type": self._index_type, "ann_index_min_rows": self._ann_min_rows}

    def _arrow_type(self, ftype: FieldType) -> Any:
        pa = self._pa
        return {"str": pa.string(), "int": pa.int64(), "float": pa.float64(), "bool": pa.bool_()}[ftype]

    def _field_type(self, arrow_type: Any) -> FieldType | None:
        types = self._pa.types
        if types.is_string(arrow_type) or types.is_large_string(arrow_type):
            return "str"
        if types.is_int64(arrow_type):
            return "int"
        if types.is_float64(arrow_type):
            return "float"
        if types.is_boolean(arrow_type):
            return "bool"
        return None

    def _entry(self, name: str, table: Any) -> CatalogEntry:
        """What RAGCentre knows about ``table``: schema metadata (metric, options) plus its columns."""
        schema = table.schema
        raw = (schema.metadata or {}).get(META_KEY)
        if raw is None or ID_COL not in schema.names or VECTOR_COL not in schema.names:
            raise StoreError(f"lancedb table for collection {name} was not created by RAGCentre")
        try:
            meta = json.loads(raw)
            metric = meta["metric"]
            options = dict(meta.get("options") or {})
        except (ValueError, KeyError, TypeError) as exc:
            raise StoreError(f"lancedb table for collection {name} has unreadable RAGCentre metadata: {exc}") from exc
        vector_type = schema.field(VECTOR_COL).type
        if not self._pa.types.is_fixed_size_list(vector_type) or metric not in ("cosine", "l2", "dot"):
            raise StoreError(f"lancedb table for collection {name} was not created by RAGCentre")
        fields: dict[str, FieldType] = {}
        for col in schema:
            if col.name in (ID_COL, VECTOR_COL):
                continue
            ftype = self._field_type(col.type)
            if ftype is None:
                raise StoreError(f"lancedb collection {name} has column {col.name} of unsupported type {col.type}")
            fields[col.name] = ftype
        return CatalogEntry(dim=int(vector_type.list_size), metric=metric, fields=fields, options=options)

    def _read(self, name: str) -> tuple[Any, CatalogEntry] | None:
        physical = self._physical(name)
        try:
            table = self._db.open_table(physical)
        except Exception as exc:
            if _is_missing(exc):
                return None
            raise StoreError(f"lancedb: could not open collection {name}: {exc}") from exc
        return table, self._entry(name, table)

    def _require(self, name: str) -> tuple[Any, CatalogEntry]:
        found = self._read(name)
        if found is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        return found

    def _call(self, what: str, name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except StoreError:
            raise
        except Exception as exc:
            if _is_missing(exc):
                raise CollectionNotFound(f"collection {name} does not exist") from exc
            raise StoreError(f"lancedb: {what} on {name} failed: {exc}") from exc

    def _schema(self, spec: CollectionSpec) -> Any:
        pa = self._pa
        meta = _meta_json(spec.metric, self._options())
        columns = [pa.field(ID_COL, pa.string(), nullable=False)]
        columns.append(pa.field(VECTOR_COL, pa.list_(pa.float32(), spec.dim), nullable=False))
        columns.extend(pa.field(fname, self._arrow_type(ftype)) for fname, ftype in spec.fields.items())
        return pa.schema(columns, metadata={META_KEY: meta})

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        if self._local_path is not None:
            path = Path(self._local_path)
            if not path.is_dir() or not os.access(path, os.W_OK):
                raise StoreError(f"lancedb is not reachable: {path} is not a writable directory")
        try:
            self._db.list_tables(limit=1)
        except Exception as exc:
            raise StoreError(f"lancedb is not reachable: {exc}") from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        bad = [f for f in spec.fields if f.startswith(RESERVED_PREFIX)]
        if bad:
            raise StoreError(f"field names starting with {RESERVED_PREFIX!r} are reserved by the lancedb store: {bad}")
        spec = replace(spec, fields={"document_id": "str", **spec.fields})
        options = self._options()
        with self._ensure_lock:
            found = self._read(spec.name)
            if found is None:
                try:
                    table = self._db.create_table(self._physical(spec.name), schema=self._schema(spec))
                except Exception as exc:
                    found = self._read(spec.name)  # created concurrently (another process)?
                    if found is None:
                        raise StoreError(f"lancedb: could not create collection {spec.name}: {exc}") from exc
                else:
                    self._create_id_index(spec.name, table)
                    return
            table, existing = found
            merged = reconcile(existing, spec, options)  # raises CollectionMismatch
            added = {f: t for f, t in merged.fields.items() if f not in existing.fields}
            if added:
                self._add_columns(spec.name, table, added)

    def _create_id_index(self, name: str, table: Any) -> None:
        # Speeds up the merge_insert and delete lookups by id. Search is correct without it.
        try:
            table.create_index(ID_COL, config=self._index.BTree(), name=ID_INDEX_NAME)
        except Exception as exc:
            log.warning("could not create the id index for %s: %s", name, exc)

    def _add_columns(self, name: str, table: Any, added: dict[str, FieldType]) -> None:
        pa = self._pa
        try:
            table.add_columns([pa.field(f, self._arrow_type(t)) for f, t in added.items()])
        except Exception as exc:
            # Another process may have added the same fields first; accept that, fail otherwise.
            current = self._require(name)[1]
            missing = {f for f in added if f not in current.fields}
            wrong = {f for f, t in added.items() if f in current.fields and current.fields[f] != t}
            if wrong:
                raise CollectionMismatch(f"field types changed concurrently in {name}: {sorted(wrong)}") from exc
            if missing:
                raise StoreError(f"lancedb: could not add fields {sorted(missing)} to {name}: {exc}") from exc

    def collection_info(self, name: str) -> CollectionInfo | None:
        check_collection_name(name)
        found = self._read(name)
        if found is None:
            return None
        table, entry = found
        count = self._call("count", name, table.count_rows)
        return CollectionInfo(dim=entry.dim, metric=entry.metric, count=int(count))

    def drop_collection(self, name: str) -> None:
        check_collection_name(name)
        with self._counter_lock:
            self._upserts.pop(name, None)
        try:
            self._db.drop_table(self._physical(name), ignore_missing=True)
        except Exception as exc:
            if not _is_missing(exc):
                raise StoreError(f"lancedb: could not drop collection {name}: {exc}") from exc

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        table, entry = self._require(name)
        if not records:
            return
        pa = self._pa
        unique: dict[str, Record] = {}
        for rec in records:  # one source row per id (a merge needs that); the last one wins
            if len(rec.vector) != entry.dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {entry.dim}")
            if not all(math.isfinite(x) for x in rec.vector):
                raise StoreError(f"record {rec.id} has a non-finite vector component")
            unique[rec.id] = rec
        rows = list(unique.values())
        flat = [x for rec in rows for x in rec.vector]
        try:
            columns: dict[str, Any] = {
                ID_COL: pa.array([r.id for r in rows], pa.string()),
                VECTOR_COL: pa.FixedSizeListArray.from_arrays(pa.array(flat, pa.float32()), entry.dim),
            }
            for fname, ftype in entry.fields.items():  # keys outside the schema are not filterable: dropped
                values = [r.metadata.get(fname) for r in rows]
                if ftype == "float":
                    values = [None if v is None else float(v) for v in values]
                columns[fname] = pa.array(values, self._arrow_type(ftype))
            data = pa.table(columns, schema=table.schema.remove_metadata())
        except Exception as exc:
            raise StoreError(f"lancedb: a record does not fit the schema of {name}: {exc}") from exc
        self._call(
            "upsert", name,
            lambda: table.merge_insert(ID_COL).when_matched_update_all().when_not_matched_insert_all().execute(data),
        )  # fmt: skip
        self._maintain(name, table, entry)

    def _maintain(self, name: str, table: Any, entry: CatalogEntry) -> None:
        """Build or refresh the ANN index and compact fragments. Never fails an upsert: search stays correct."""
        with self._counter_lock:
            n = self._upserts[name] = self._upserts.get(name, 0) + 1
        try:
            optimize = n % OPTIMIZE_EVERY == 0
            if self._ann_min_rows is not None and table.count_rows() >= self._ann_min_rows:
                stats = self._index_stats(table)
                if stats is None:
                    self._build_index(table, entry.metric)
                elif stats.num_unindexed_rows > max(stats.num_indexed_rows, self._ann_min_rows):
                    optimize = True
            if optimize:
                table.optimize()
        except Exception as exc:
            log.warning("index maintenance on %s failed (queries stay correct): %s", name, exc)

    def _build_index(self, table: Any, metric: Metric) -> None:
        config = getattr(self._index, _INDEX_CLASS[self._index_type])(distance_type=metric)
        table.create_index(VECTOR_COL, config=config, name=INDEX_NAME, replace=True)

    @staticmethod
    def _index_stats(table: Any) -> Any:
        if not any(ix.name == INDEX_NAME for ix in table.list_indices()):
            return None
        return table.index_stats(INDEX_NAME)

    def delete(self, name: str, ids: Sequence[str]) -> None:
        check_collection_name(name)
        if not ids:
            return
        found = self._read(name)
        if found is None:
            return
        table, _ = found
        listed = ", ".join(string_literal(i) for i in ids)
        self._call("delete", name, table.delete, f"`{ID_COL}` IN ({listed})")

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        table, entry = self._require(name)
        if len(vector) != entry.dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {entry.dim}")
        if k < 1:
            return []
        where: str | None = None
        if filter is not None:
            try:
                where = compile_datafusion(filter, allowed=set(entry.fields))
            except ValueError as exc:
                raise StoreError(f"lancedb: cannot compile the filter: {exc}") from exc
        rows = self._search(name, table, entry, vector, k, where, bypass_index=False)
        # An ANN index may under-fill a filtered query; the exact scan of the filtered rows cannot.
        if where is not None and len(rows) < k and self.capabilities.filter_exact and self._index_stats(table):
            rows = self._search(name, table, entry, vector, k, where, bypass_index=True)
        return [Match(id=i, score=score_from_distance(entry.metric, d)) for i, d in rows]

    def _search(
        self, name: str, table: Any, entry: CatalogEntry, vector: Sequence[float], k: int,
        where: str | None, *, bypass_index: bool,
    ) -> list[tuple[str, float]]:  # fmt: skip
        def run() -> Any:
            q = table.search([float(x) for x in vector], vector_column_name=VECTOR_COL).distance_type(entry.metric)
            if where is not None:
                q = q.where(where, prefilter=True)
            if bypass_index:
                q = q.bypass_vector_index()
            elif self._nprobes is not None:
                q = q.nprobes(self._nprobes)
            return q.select([ID_COL, DISTANCE_COL]).limit(k).to_arrow()

        result = self._call("query", name, run)
        return list(zip(result.column(ID_COL).to_pylist(), result.column(DISTANCE_COL).to_pylist(), strict=True))

    def close(self) -> None:
        close = getattr(self._db, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                close()


def _meta_json(metric: Metric, options: dict[str, Any]) -> str:
    return json.dumps({"metric": metric, "options": options}, sort_keys=True, separators=(",", ":"))


def _safe_uri(uri: str) -> str:
    return uri.split("?", 1)[0]


# ---- registration ---------------------------------------------------------------------------


class LanceDBConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uri: str | None = Field(
        default=None,
        description="Local directory, object-store URI (s3://, gs://, az://) or LanceDB Cloud (db://name). "
        "Default: a 'lancedb' directory in the data dir. A relative path is placed in the data dir.",
    )
    region: str | None = Field(default=None, description="Cloud region (db://) or object-store region")
    ann_index_min_rows: int | None = Field(
        default=50_000,
        ge=MIN_ANN_ROWS,
        description="Build an ANN index once a collection has this many rows; below it search is exact. "
        "Empty means never build one (set once)",
    )
    index_type: IndexType = Field(default="IVF_FLAT", description="ANN index type, used with the threshold (set once)")
    nprobes: int | None = Field(default=None, ge=1, description="IVF partitions to search (query time)")
    prefix: str = Field(default="", description="Prefix prepended to the LanceDB table name")

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        if not _PREFIX_RE.match(v):
            raise ValueError("prefix may contain only letters, digits, '_', '-' and '.'")
        return v

    @field_validator("uri")
    @classmethod
    def _check_uri(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("uri must not be empty")
        return v


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> LanceDBStore:
    assert isinstance(config, LanceDBConfig)
    uri = config.uri if config.uri is not None else "lancedb"
    if "://" not in uri and not Path(uri).is_absolute():
        uri = str(data_dir / uri)
    return LanceDBStore.connect(
        uri,
        api_key=secrets.get("api_key") or None,
        region=config.region,
        prefix=config.prefix,
        ann_index_min_rows=config.ann_index_min_rows,
        index_type=config.index_type,
        nprobes=config.nprobes,
    )


STORE_TYPE = VectorStoreType(
    type="lancedb",
    label="LanceDB",
    description="LanceDB in a local directory, on object storage, or LanceDB Cloud. Needs the optional 'lancedb' extra.",
    config_model=LanceDBConfig,
    secret_keys=("api_key",),
    capabilities=CAPABILITIES,
    factory=_factory,
    immutable_fields=("ann_index_min_rows", "index_type"),
)
