"""PostgreSQL + pgvector store (optional extra ``ragcentre[pgvector]``).

One table per collection in the configured schema::

    (id uuid PRIMARY KEY, embedding vector(<dim>) NOT NULL, metadata jsonb NOT NULL)

or ``halfvec(<dim>)`` above 2000 dimensions (``vector_type="auto"``). An HNSW index using the
metric's operator class and a GIN index on ``metadata`` are created when they fit. Dimension,
metric, fields and the index-shaping options (``vector_type``, ``hnsw_m``,
``hnsw_ef_construction``) are recorded in ``<schema>._collections``. Filters compile to SQL
through ``filter_sql.compile_postgres``.

Filtered HNSW scans can return fewer than ``k`` rows. With pgvector 0.8 or later each query sets
``hnsw.iterative_scan = strict_order`` for its transaction, so the index keeps scanning until
``k`` rows pass the filter. Older versions only get ``hnsw.ef_search = 1000``, which makes a
short result much less likely for very selective filters but cannot rule it out.

HNSW dimension limits: ``vector`` up to 2000, ``halfvec`` up to 4000. ``vector_type="auto"``
uses ``halfvec`` above 2000 dims, and falls back to an exact scan (no HNSW index) above 4000 or
when ``vector`` is chosen for a collection over 2000 dims. The ``vector``/``halfvec`` type limit
is 16000 dimensions.

``psycopg`` is imported only when a store is created, so ``import ragcentre`` works without the
extra. Vectors are sent in pgvector's text form and cast with ``::vector``/``::halfvec``, so
numpy is not needed.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
import threading
import warnings
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

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
from ragcentre.stores.filter_sql import compile_postgres
from ragcentre.stores.registry import VectorStoreType

log = logging.getLogger("ragcentre.stores.pgvector")

SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
HNSW_MAX_DIM = 2000  # HNSW index limit for the vector type
HALFVEC_HNSW_MAX_DIM = 4000  # HNSW index limit for the halfvec type
VECTOR_MAX_DIM = 16000  # type limit for both vector and halfvec
POOL_SIZE = 4
DEFAULT_HNSW_M = 16
DEFAULT_HNSW_EF_CONSTRUCTION = 64
VectorType = Literal["auto", "vector", "halfvec"]
_OPS: dict[Metric, str] = {  # distance operator (shared by vector and halfvec)
    "cosine": "<=>",
    "l2": "<->",
    "dot": "<#>",
}
_HNSW_OPCLASS: dict[str, dict[Metric, str]] = {
    "vector": {"cosine": "vector_cosine_ops", "l2": "vector_l2_ops", "dot": "vector_ip_ops"},
    "halfvec": {"cosine": "halfvec_cosine_ops", "l2": "halfvec_l2_ops", "dot": "halfvec_ip_ops"},
}


class _Meta(NamedTuple):
    dim: int
    metric: Metric
    fields: dict[str, FieldType]
    vector_type: str  # "vector" | "halfvec" (the effective type the table was built with)
    hnsw_m: int
    hnsw_ef_construction: int


def _import() -> Any:
    return require_extra("psycopg", store="pgvector", extra="pgvector", package="psycopg")


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _score(metric: Metric, value: float) -> float:
    if math.isnan(value):  # cosine distance to a zero vector: treat as orthogonal
        return 0.0
    if metric == "cosine":
        return 1.0 - value
    return -value  # l2: negated distance; dot: <#> is the negative inner product


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


class _Pool:
    """A tiny connection pool: up to ``size`` idle autocommit connections are kept for reuse."""

    def __init__(self, psycopg: Any, dsn: str, size: int) -> None:
        self._psycopg = psycopg
        self._dsn = dsn
        self._size = size
        self._idle: list[Any] = []
        self._lock = threading.Lock()
        self._closed = False

    @contextlib.contextmanager
    def connection(self) -> Iterator[Any]:
        with self._lock:
            if self._closed:
                raise StoreError("pgvector store is closed")
            conn = self._idle.pop() if self._idle else None
        if conn is None or conn.closed:
            conn = self._psycopg.connect(self._dsn, autocommit=True)
        ok = False
        try:
            yield conn
            ok = True
        finally:
            broken = conn.closed or conn.info.transaction_status != self._psycopg.pq.TransactionStatus.IDLE
            with self._lock:
                keep = ok and not broken and not self._closed and len(self._idle) < self._size
                if keep:
                    self._idle.append(conn)
            if not keep:
                with contextlib.suppress(Exception):
                    conn.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for conn in idle:
            with contextlib.suppress(Exception):
                conn.close()


class PgvectorStore:
    capabilities: ClassVar[Capabilities] = Capabilities(
        metrics=frozenset({"cosine", "l2", "dot"}), max_batch=500, max_dim=VECTOR_MAX_DIM
    )

    def __init__(
        self,
        dsn: str,
        schema: str = "ragcentre",
        pool_size: int = POOL_SIZE,
        *,
        vector_type: VectorType = "auto",
        hnsw_m: int | None = None,
        hnsw_ef_construction: int | None = None,
        ef_search: int | None = None,
        max_scan_tuples: int | None = None,
    ) -> None:
        self._pg = _import()
        from psycopg import sql

        if not SCHEMA_RE.match(schema):
            raise StoreError(f"invalid schema name {schema!r}")
        self._sql = sql
        self.schema = schema
        self.vector_type = vector_type
        self.hnsw_m = hnsw_m
        self.hnsw_ef_construction = hnsw_ef_construction
        self.ef_search = ef_search
        self.max_scan_tuples = max_scan_tuples
        self._pool = _Pool(self._pg, dsn, pool_size)
        self._meta_cache: dict[str, _Meta] = {}
        self._cache_lock = threading.Lock()
        self._iterative_scan = False
        self._setup()

    def _effective_vector_type(self, dim: int) -> str:
        """The column type a collection of ``dim`` dimensions gets under this store's config."""
        if self.vector_type == "auto":
            return "halfvec" if dim > HNSW_MAX_DIM else "vector"
        return self.vector_type

    # ---- helpers ------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _conn(self, what: str) -> Iterator[Any]:
        try:
            with self._pool.connection() as conn:
                yield conn
        except StoreError:
            raise
        except self._pg.errors.UndefinedTable as exc:
            raise CollectionNotFound(f"pgvector: {what}: {exc}") from exc
        except self._pg.Error as exc:
            raise StoreError(f"pgvector: {what} failed: {exc}") from exc

    def _table(self, name: str) -> Any:
        return self._sql.Identifier(self.schema, name)

    def _setup(self) -> None:
        s = self._sql
        with self._conn("setup") as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('ragcentre:pgvector:setup'))")
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(s.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(s.Identifier(self.schema)))
            conn.execute(
                s.SQL(
                    "CREATE TABLE IF NOT EXISTS {} (name text PRIMARY KEY, dim integer NOT NULL, "
                    "metric text NOT NULL, fields jsonb NOT NULL, vector_type text, "
                    "hnsw_m integer, hnsw_ef_construction integer)"
                ).format(self._table("_collections"))
            )
            # Migrate collections created before the index-shaping options were recorded: a NULL
            # vector_type reads as "vector", NULL hnsw_* read as the pgvector defaults.
            conn.execute(
                s.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS vector_type text").format(self._table("_collections"))
            )
            conn.execute(
                s.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS hnsw_m integer").format(self._table("_collections"))
            )
            conn.execute(
                s.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS hnsw_ef_construction integer").format(
                    self._table("_collections")
                )
            )
            row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        self.extension_version = str(row[0]) if row else ""
        self._iterative_scan = _version_tuple(self.extension_version) >= (0, 8, 0)

    def _read_meta(self, conn: Any, name: str) -> _Meta | None:
        row = conn.execute(
            self._sql.SQL(
                "SELECT dim, metric, fields, vector_type, hnsw_m, hnsw_ef_construction FROM {} WHERE name = %s"
            ).format(self._table("_collections")),
            (name,),
        ).fetchone()
        if row is None:
            return None
        return _Meta(
            dim=int(row[0]),
            metric=row[1],
            fields=dict(row[2]),
            vector_type=row[3] or "vector",
            hnsw_m=int(row[4]) if row[4] is not None else DEFAULT_HNSW_M,
            hnsw_ef_construction=int(row[5]) if row[5] is not None else DEFAULT_HNSW_EF_CONSTRUCTION,
        )

    def _meta(self, name: str) -> _Meta:
        check_collection_name(name)
        with self._cache_lock:
            cached = self._meta_cache.get(name)
        if cached is not None:
            return cached
        with self._conn(f"read collection {name}") as conn:
            meta = self._read_meta(conn, name)
        if meta is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        with self._cache_lock:
            self._meta_cache[name] = meta
        return meta

    def _forget(self, name: str) -> None:
        with self._cache_lock:
            self._meta_cache.pop(name, None)

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        with self._conn("healthcheck") as conn:
            row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        if row is None:
            raise StoreError("pgvector: the vector extension is not installed in this database")

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        s = self._sql
        table = self._table(spec.name)
        vector_type = self._effective_vector_type(spec.dim)
        m = self.hnsw_m if self.hnsw_m is not None else DEFAULT_HNSW_M
        ef_construction = (
            self.hnsw_ef_construction if self.hnsw_ef_construction is not None else DEFAULT_HNSW_EF_CONSTRUCTION
        )
        self._forget(spec.name)
        with self._conn(f"ensure collection {spec.name}") as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"ragcentre:{self.schema}.{spec.name}",))
            meta = self._read_meta(conn, spec.name)
            if meta is None:
                conn.execute(
                    s.SQL(
                        "CREATE TABLE {} (id uuid PRIMARY KEY, embedding {}({}) NOT NULL, metadata jsonb NOT NULL)"
                    ).format(table, s.SQL(vector_type), s.Literal(spec.dim))
                )
                hnsw_limit = HALFVEC_HNSW_MAX_DIM if vector_type == "halfvec" else HNSW_MAX_DIM
                if spec.dim <= hnsw_limit:
                    opclass = _HNSW_OPCLASS[vector_type][spec.metric]
                    opts: list[Any] = []
                    if self.hnsw_m is not None:
                        opts.append(s.SQL("m = {}").format(s.Literal(self.hnsw_m)))
                    if self.hnsw_ef_construction is not None:
                        opts.append(s.SQL("ef_construction = {}").format(s.Literal(self.hnsw_ef_construction)))
                    if opts:
                        conn.execute(
                            s.SQL("CREATE INDEX ON {} USING hnsw (embedding {}) WITH ({})").format(
                                table, s.SQL(opclass), s.SQL(", ").join(opts)
                            )
                        )
                    else:
                        conn.execute(
                            s.SQL("CREATE INDEX ON {} USING hnsw (embedding {})").format(table, s.SQL(opclass))
                        )
                else:
                    log.warning(
                        "dimension above pgvector's HNSW limit for %s; queries will use an exact scan",
                        vector_type,
                        extra={
                            "collection": spec.name,
                            "dim": spec.dim,
                            "limit": hnsw_limit,
                            "vector_type": vector_type,
                        },
                    )
                conn.execute(s.SQL("CREATE INDEX ON {} USING gin (metadata)").format(table))
                conn.execute(
                    s.SQL(
                        "INSERT INTO {} (name, dim, metric, fields, vector_type, hnsw_m, hnsw_ef_construction) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)"
                    ).format(self._table("_collections")),
                    (
                        spec.name,
                        spec.dim,
                        spec.metric,
                        self._pg.types.json.Jsonb(dict(spec.fields)),
                        vector_type,
                        m,
                        ef_construction,
                    ),
                )
                return
            if meta.dim != spec.dim or meta.metric != spec.metric:
                raise CollectionMismatch(
                    f"collection {spec.name} exists with dim={meta.dim}, metric={meta.metric}; "
                    f"requested dim={spec.dim}, metric={spec.metric}"
                )
            if meta.vector_type != vector_type:
                raise CollectionMismatch(
                    f"collection {spec.name} uses vector type {meta.vector_type}; requested {vector_type}"
                )
            if meta.hnsw_m != m or meta.hnsw_ef_construction != ef_construction:
                raise CollectionMismatch(
                    f"collection {spec.name} was built with hnsw m={meta.hnsw_m}, "
                    f"ef_construction={meta.hnsw_ef_construction}; requested m={m}, ef_construction={ef_construction}"
                )
            for fname, ftype in spec.fields.items():
                if fname in meta.fields and meta.fields[fname] != ftype:
                    raise CollectionMismatch(f"field {fname} is {meta.fields[fname]} in {spec.name}, not {ftype}")
            merged = {**meta.fields, **spec.fields}
            if merged != meta.fields:
                conn.execute(
                    s.SQL("UPDATE {} SET fields = %s WHERE name = %s").format(self._table("_collections")),
                    (self._pg.types.json.Jsonb(merged), spec.name),
                )

    def collection_info(self, name: str) -> CollectionInfo | None:
        check_collection_name(name)
        with self._conn(f"collection info {name}") as conn:
            meta = self._read_meta(conn, name)
            if meta is None:
                return None
            row = conn.execute(self._sql.SQL("SELECT count(*) FROM {}").format(self._table(name))).fetchone()
        return CollectionInfo(dim=meta.dim, metric=meta.metric, count=int(row[0]))

    def drop_collection(self, name: str) -> None:
        check_collection_name(name)
        self._forget(name)
        with self._conn(f"drop collection {name}") as conn, conn.transaction():
            conn.execute(self._sql.SQL("DROP TABLE IF EXISTS {}").format(self._table(name)))
            conn.execute(self._sql.SQL("DELETE FROM {} WHERE name = %s").format(self._table("_collections")), (name,))

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        meta = self._meta(name)
        dim = meta.dim
        vector_type = meta.vector_type
        rows = []
        for rec in records:
            if len(rec.vector) != dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {dim}")
            rows.append((rec.id, vector_literal(rec.vector), self._pg.types.json.Jsonb(dict(rec.metadata))))
        if not rows:
            return
        stmt = self._sql.SQL(
            "INSERT INTO {} (id, embedding, metadata) VALUES (%s, %s::{}, %s) "
            "ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding, metadata = EXCLUDED.metadata"
        ).format(self._table(name), self._sql.SQL(vector_type))
        with self._conn(f"upsert into {name}") as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany(stmt, rows)

    def delete(self, name: str, ids: Sequence[str]) -> None:
        check_collection_name(name)
        if not ids:
            return
        try:
            self._meta(name)
        except CollectionNotFound:
            return
        stmt = self._sql.SQL("DELETE FROM {} WHERE id = ANY(%s::uuid[])").format(self._table(name))
        with contextlib.suppress(CollectionNotFound), self._conn(f"delete from {name}") as conn:
            conn.execute(stmt, (list(ids),))

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        meta = self._meta(name)
        dim = meta.dim
        metric = meta.metric
        fields = meta.fields
        vector_type = meta.vector_type
        if len(vector) != dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {dim}")
        if k < 1:
            return []
        s = self._sql
        where, params = compile_postgres(filter, {**fields, "document_id": "str"})
        distance = s.SQL("embedding {} %s::{}").format(s.SQL(_OPS[metric]), s.SQL(vector_type))
        stmt = s.SQL("SELECT id::text, {} FROM {} WHERE {} ORDER BY {} LIMIT %s").format(
            distance,
            self._table(name),
            s.SQL(where),
            distance,
        )
        q = vector_literal(vector)
        with self._conn(f"query {name}") as conn, conn.transaction():
            if self.ef_search is not None:
                ef_search = self.ef_search
            elif self._iterative_scan:
                ef_search = max(40, min(k, 1000))
            else:
                ef_search = 1000
            if self._iterative_scan:
                conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
                if self.max_scan_tuples is not None:
                    conn.execute(s.SQL("SET LOCAL hnsw.max_scan_tuples = {}").format(s.Literal(self.max_scan_tuples)))
            elif self.max_scan_tuples is not None:
                log.warning(
                    "max_scan_tuples is ignored: pgvector %s has no iterative index scans",
                    self.extension_version,
                    extra={"collection": name},
                )
            conn.execute(s.SQL("SET LOCAL hnsw.ef_search = {}").format(s.Literal(ef_search)))
            rows = conn.execute(stmt, [q, *params, q, k]).fetchall()
        return [Match(id=row[0], score=_score(metric, float(row[1]))) for row in rows]

    def close(self) -> None:
        self._pool.close()


# ---- registration ---------------------------------------------------------------------------


with warnings.catch_warnings():
    # The field is called "schema" as documented; it shadows the deprecated BaseModel.schema().
    warnings.filterwarnings("ignore", message=".*shadows an attribute in parent.*")

    class PgvectorConfig(BaseModel):
        model_config = ConfigDict(extra="forbid")
        schema: str = Field(  # type: ignore[assignment,unused-ignore]
            default="ragcentre",
            pattern=SCHEMA_RE.pattern,
            description="PostgreSQL schema for collection tables (created if missing)",
        )
        vector_type: Literal["auto", "vector", "halfvec"] = Field(
            default="auto",
            description="Column type: 'auto' uses halfvec above 2000 dims (HNSW to 4000). Immutable.",
        )
        hnsw_m: int | None = Field(
            default=None, ge=2, le=100, description="HNSW index 'm' parameter (default 16). Immutable."
        )
        hnsw_ef_construction: int | None = Field(
            default=None, ge=4, le=1000, description="HNSW index 'ef_construction' (default 64). Immutable."
        )
        ef_search: int | None = Field(
            default=None, ge=1, le=1000, description="Query-time hnsw.ef_search (default: 40 or k, clamped to 1000)."
        )
        max_scan_tuples: int | None = Field(
            default=None, ge=1, description="Query-time hnsw.max_scan_tuples for iterative filtered scans."
        )
        host: str | None = Field(default=None, description="PostgreSQL host (alternative to the 'dsn' secret)")
        port: int | None = Field(default=None, ge=1, le=65535, description="PostgreSQL port")
        dbname: str | None = Field(default=None, description="PostgreSQL database name")
        user: str | None = Field(default=None, description="PostgreSQL user")
        sslmode: Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] | None = Field(
            default=None, description="PostgreSQL SSL mode"
        )


def _conninfo_from_parts(config: PgvectorConfig, password: str | None) -> str | None:
    """Build a libpq connection string from the host/port/... fields, or ``None`` when unusable."""
    if not config.host:
        return None
    from psycopg import conninfo

    return conninfo.make_conninfo(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=password,
        sslmode=config.sslmode,
    )


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> PgvectorStore:
    assert isinstance(config, PgvectorConfig)
    dsn = secrets.get("dsn") or _conninfo_from_parts(config, secrets.get("password"))
    if not dsn:
        raise StoreError(
            "the pgvector store needs its 'dsn' secret (a PostgreSQL connection string) or a 'host' "
            "field (with the optional 'password' secret)"
        )
    return PgvectorStore(
        dsn,
        schema=config.schema,
        vector_type=config.vector_type,
        hnsw_m=config.hnsw_m,
        hnsw_ef_construction=config.hnsw_ef_construction,
        ef_search=config.ef_search,
        max_scan_tuples=config.max_scan_tuples,
    )


STORE_TYPE = VectorStoreType(
    type="pgvector",
    label="PostgreSQL (pgvector)",
    description="PostgreSQL with the pgvector extension and HNSW indexes. Needs the optional 'pgvector' extra.",
    config_model=PgvectorConfig,
    secret_keys=("dsn", "password"),
    capabilities=PgvectorStore.capabilities,
    factory=_factory,
    immutable_fields=("vector_type", "hnsw_m", "hnsw_ef_construction"),
)
