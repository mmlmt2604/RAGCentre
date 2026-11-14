"""PostgreSQL + pgvector store (optional extra ``ragcentre[pgvector]``).

One table per collection in the configured schema::

    (id uuid PRIMARY KEY, embedding vector(<dim>) NOT NULL, metadata jsonb NOT NULL)

with an HNSW index using the metric's operator class and a GIN index on ``metadata``. Dimension,
metric and fields are recorded in ``<schema>._collections``. Filters compile to SQL through
``filter_sql.compile_postgres``.

Filtered HNSW scans can return fewer than ``k`` rows. With pgvector 0.8 or later each query sets
``hnsw.iterative_scan = strict_order`` for its transaction, so the index keeps scanning until
``k`` rows pass the filter. Older versions only get ``hnsw.ef_search = 1000``, which makes a
short result much less likely for very selective filters but cannot rule it out.

pgvector's HNSW index supports at most 2000 dimensions for the ``vector`` type; above that the
index is skipped (with a warning) and queries are exact scans.

``psycopg`` is imported only when a store is created, so ``import ragcentre`` works without the
extra. Vectors are sent in pgvector's text form and cast with ``::vector``, so numpy is not needed.
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
from typing import Any, ClassVar

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
HNSW_MAX_DIM = 2000
POOL_SIZE = 4
_OPS: dict[Metric, tuple[str, str]] = {  # operator, HNSW operator class
    "cosine": ("<=>", "vector_cosine_ops"),
    "l2": ("<->", "vector_l2_ops"),
    "dot": ("<#>", "vector_ip_ops"),
}


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
    capabilities: ClassVar[Capabilities] = Capabilities(metrics=frozenset({"cosine", "l2", "dot"}), max_batch=500)

    def __init__(self, dsn: str, schema: str = "ragcentre", pool_size: int = POOL_SIZE) -> None:
        self._pg = _import()
        from psycopg import sql

        if not SCHEMA_RE.match(schema):
            raise StoreError(f"invalid schema name {schema!r}")
        self._sql = sql
        self.schema = schema
        self._pool = _Pool(self._pg, dsn, pool_size)
        self._meta_cache: dict[str, tuple[int, Metric, dict[str, FieldType]]] = {}
        self._cache_lock = threading.Lock()
        self._iterative_scan = False
        self._setup()

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
                    "metric text NOT NULL, fields jsonb NOT NULL)"
                ).format(self._table("_collections"))
            )
            row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        self.extension_version = str(row[0]) if row else ""
        self._iterative_scan = _version_tuple(self.extension_version) >= (0, 8, 0)

    def _read_meta(self, conn: Any, name: str) -> tuple[int, Metric, dict[str, FieldType]] | None:
        row = conn.execute(
            self._sql.SQL("SELECT dim, metric, fields FROM {} WHERE name = %s").format(self._table("_collections")),
            (name,),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), row[1], dict(row[2])

    def _meta(self, name: str) -> tuple[int, Metric, dict[str, FieldType]]:
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
        self._forget(spec.name)
        with self._conn(f"ensure collection {spec.name}") as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"ragcentre:{self.schema}.{spec.name}",))
            meta = self._read_meta(conn, spec.name)
            if meta is None:
                conn.execute(
                    s.SQL(
                        "CREATE TABLE {} (id uuid PRIMARY KEY, embedding vector({}) NOT NULL, metadata jsonb NOT NULL)"
                    ).format(table, s.Literal(spec.dim))
                )
                if spec.dim <= HNSW_MAX_DIM:
                    conn.execute(
                        s.SQL("CREATE INDEX ON {} USING hnsw (embedding {})").format(table, s.SQL(_OPS[spec.metric][1]))
                    )
                else:
                    log.warning(
                        "dimension above pgvector's HNSW limit; queries will use an exact scan",
                        extra={"collection": spec.name, "dim": spec.dim, "limit": HNSW_MAX_DIM},
                    )
                conn.execute(s.SQL("CREATE INDEX ON {} USING gin (metadata)").format(table))
                conn.execute(
                    s.SQL("INSERT INTO {} (name, dim, metric, fields) VALUES (%s, %s, %s, %s)").format(
                        self._table("_collections")
                    ),
                    (spec.name, spec.dim, spec.metric, self._pg.types.json.Jsonb(dict(spec.fields))),
                )
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
        return CollectionInfo(dim=meta[0], metric=meta[1], count=int(row[0]))

    def drop_collection(self, name: str) -> None:
        check_collection_name(name)
        self._forget(name)
        with self._conn(f"drop collection {name}") as conn, conn.transaction():
            conn.execute(self._sql.SQL("DROP TABLE IF EXISTS {}").format(self._table(name)))
            conn.execute(self._sql.SQL("DELETE FROM {} WHERE name = %s").format(self._table("_collections")), (name,))

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        dim, _, _ = self._meta(name)
        rows = []
        for rec in records:
            if len(rec.vector) != dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {dim}")
            rows.append((rec.id, vector_literal(rec.vector), self._pg.types.json.Jsonb(dict(rec.metadata))))
        if not rows:
            return
        stmt = self._sql.SQL(
            "INSERT INTO {} (id, embedding, metadata) VALUES (%s, %s::vector, %s) "
            "ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding, metadata = EXCLUDED.metadata"
        ).format(self._table(name))
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
        dim, metric, fields = self._meta(name)
        if len(vector) != dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {dim}")
        if k < 1:
            return []
        s = self._sql
        where, params = compile_postgres(filter, {**fields, "document_id": "str"})
        distance = s.SQL("embedding {} %s::vector").format(s.SQL(_OPS[metric][0]))
        stmt = s.SQL("SELECT id::text, {} FROM {} WHERE {} ORDER BY {} LIMIT %s").format(
            distance,
            self._table(name),
            s.SQL(where),
            distance,
        )
        q = vector_literal(vector)
        with self._conn(f"query {name}") as conn, conn.transaction():
            if self._iterative_scan:
                conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
                conn.execute(s.SQL("SET LOCAL hnsw.ef_search = {}").format(s.Literal(max(40, min(k, 1000)))))
            else:
                conn.execute("SET LOCAL hnsw.ef_search = 1000")
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


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> PgvectorStore:
    assert isinstance(config, PgvectorConfig)
    dsn = secrets.get("dsn")
    if not dsn:
        raise StoreError("the pgvector store needs its 'dsn' secret (a PostgreSQL connection string)")
    return PgvectorStore(dsn, schema=config.schema)


STORE_TYPE = VectorStoreType(
    type="pgvector",
    label="PostgreSQL (pgvector)",
    description="PostgreSQL with the pgvector extension and HNSW indexes. Needs the optional 'pgvector' extra.",
    config_model=PgvectorConfig,
    secret_keys=("dsn",),
    capabilities=PgvectorStore.capabilities,
    factory=_factory,
)
