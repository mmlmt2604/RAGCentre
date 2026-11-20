"""The default vector store: ordinary SQLite tables scanned with sqlite-vec distance functions.

Each collection is one table ``(id TEXT PRIMARY KEY, vector BLOB, metadata TEXT)``; collection
dimension, metric and fields live in ``_collections``. Queries are an exact scan with the filter
compiled into the ``WHERE`` clause, so filtering happens before ranking and ``k`` matches are
returned whenever ``k`` exist. This is fast enough for roughly 100k vectors per collection; use
another adapter beyond that.

sqlite-vec 0.1.9 (the newest stable release) has ``vec_distance_cosine`` and ``vec_distance_l2``
but no dot-product distance, and ``vec_length`` returns the element count rather than the norm, so
the norms needed to derive a dot product from the two distances are not available in SQL. Only the
``cosine`` and ``l2`` metrics are offered.

``prefix`` is prepended to the logical collection name to form the physical table name, which is
also the key in ``_collections``. With the default empty prefix the physical name is the logical
name, so databases created before the option existed open unchanged.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ragcentre.db.connection import Database
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
    StoreUnavailable,
    check_collection_name,
    check_spec,
)
from ragcentre.stores.filter_sql import compile_sqlite, sqlite_json_field
from ragcentre.stores.registry import VectorStoreType
from ragcentre.vectors import pack

NO_EXTENSIONS_MESSAGE = (
    "This Python's sqlite3 module cannot load SQLite extensions (Connection.enable_load_extension is "
    "missing), so the sqlite_vec vector store cannot run. This is common with the macOS system Python. "
    "Use a Python build that supports loadable SQLite extensions, for example one installed with uv "
    "(`uv python install`), pyenv, mise or Homebrew."
)

_DISTANCE: dict[str, str] = {
    # A zero vector has no cosine distance (NULL); treat it as orthogonal (similarity 0).
    "cosine": "COALESCE(vec_distance_cosine(vector, ?), 1.0)",
    "l2": "vec_distance_l2(vector, ?)",
}


def load_extension(conn: sqlite3.Connection) -> None:
    if not hasattr(conn, "enable_load_extension"):
        raise StoreUnavailable(NO_EXTENSIONS_MESSAGE)
    import sqlite_vec

    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except (AttributeError, sqlite3.OperationalError) as exc:
        raise StoreUnavailable(f"{NO_EXTENSIONS_MESSAGE} ({exc})") from exc
    finally:
        with contextlib.suppress(AttributeError, sqlite3.Error):
            conn.enable_load_extension(False)


# SQLite table names are case-insensitive and ``sqlite_`` names are reserved, so the physical name
# is restricted to lowercase letters, digits and underscores.
_PHYSICAL_RE = re.compile(r"^[a-z_][a-z0-9_]{2,127}$")
_PREFIX_RE = re.compile(r"^[a-z0-9_]{0,64}$")
_RESERVED_TABLES = frozenset({"_collections"})


def _score(metric: str, distance: float) -> float:
    return 1.0 - distance if metric == "cosine" else -distance


class SqliteVecStore:
    # The type's best case; each instance sets its own in ``__init__``. sqlite-vec has no dimension
    # limit (its distance functions were run on vectors of 1,000,000 dimensions), so max_dim is None.
    capabilities: Capabilities = Capabilities(metrics=frozenset({"cosine", "l2"}), max_batch=1000)

    def __init__(self, path: Path | str, *, prefix: str = "") -> None:
        self.capabilities = Capabilities(metrics=frozenset({"cosine", "l2"}), max_batch=1000, max_dim=None)
        self._prefix = prefix
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = Database(self.path, setup=load_extension)
        self._write_lock = threading.Lock()
        with self._write_lock, self._db.write() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _collections ("
                " name TEXT PRIMARY KEY, dim INTEGER NOT NULL, metric TEXT NOT NULL, fields TEXT NOT NULL)"
            )

    # ---- helpers ------------------------------------------------------------------------------

    def _physical(self, name: str) -> str:
        """Map a logical collection name to its table name (Decision 4) and re-validate the result."""
        check_collection_name(name)
        if not self._prefix:
            return name
        physical = self._prefix + name
        if not _PHYSICAL_RE.match(physical) or physical in _RESERVED_TABLES or physical.startswith("sqlite_"):
            raise StoreError(
                f"table name {physical!r} (prefix {self._prefix!r} plus {name!r}) is not a valid sqlite_vec "
                "collection table name (lowercase letters, digits and '_'; not reserved)"
            )
        return physical

    def _meta(self, name: str) -> tuple[int, Metric, dict[str, FieldType]] | None:
        row = self._db.one("SELECT dim, metric, fields FROM _collections WHERE name = ?", (name,))
        if row is None:
            return None
        return int(row["dim"]), row["metric"], json.loads(row["fields"])

    def _require(self, name: str) -> tuple[str, int, Metric]:
        """The physical table, dimension and metric of an existing collection."""
        physical = self._physical(name)
        meta = self._meta(physical)
        if meta is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        return physical, meta[0], meta[1]

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        try:
            self._db.scalar("SELECT vec_version()")
        except sqlite3.Error as exc:
            raise StoreError(f"sqlite-vec store at {self.path} is not usable: {exc}") from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        physical = self._physical(spec.name)
        with self._write_lock, self._db.write() as conn:
            meta = self._meta(physical)
            if meta is None:
                conn.execute(
                    f'CREATE TABLE IF NOT EXISTS "{physical}" '
                    "(id TEXT PRIMARY KEY, vector BLOB NOT NULL, metadata TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO _collections (name, dim, metric, fields) VALUES (?, ?, ?, ?)",
                    (physical, spec.dim, spec.metric, json.dumps(spec.fields, sort_keys=True)),
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
                    "UPDATE _collections SET fields = ? WHERE name = ?", (json.dumps(merged, sort_keys=True), physical)
                )

    def collection_info(self, name: str) -> CollectionInfo | None:
        physical = self._physical(name)
        meta = self._meta(physical)
        if meta is None:
            return None
        count = int(self._db.scalar(f'SELECT COUNT(*) FROM "{physical}"'))
        return CollectionInfo(dim=meta[0], metric=meta[1], count=count)

    def drop_collection(self, name: str) -> None:
        physical = self._physical(name)
        with self._write_lock, self._db.write() as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{physical}"')
            conn.execute("DELETE FROM _collections WHERE name = ?", (physical,))

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        physical, dim, _ = self._require(name)
        rows = []
        for rec in records:
            if len(rec.vector) != dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {dim}")
            rows.append((rec.id, pack(rec.vector), json.dumps(rec.metadata, sort_keys=True)))
        with self._write_lock, self._db.write() as conn:
            conn.executemany(f'INSERT OR REPLACE INTO "{physical}" (id, vector, metadata) VALUES (?, ?, ?)', rows)

    def delete(self, name: str, ids: Sequence[str]) -> None:
        physical = self._physical(name)
        if self._meta(physical) is None or not ids:
            return
        with self._write_lock, self._db.write() as conn:
            conn.executemany(f'DELETE FROM "{physical}" WHERE id = ?', [(i,) for i in ids])

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        physical, dim, metric = self._require(name)
        if len(vector) != dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {dim}")
        if k < 1:
            return []
        where, params = compile_sqlite(filter, sqlite_json_field("metadata"))
        sql = f'SELECT id, {_DISTANCE[metric]} AS d FROM "{physical}" WHERE {where} ORDER BY d, id LIMIT ?'
        rows = self._db.all(sql, [pack(vector), *params, k])
        return [Match(id=row["id"], score=_score(metric, float(row["d"]))) for row in rows]

    def close(self) -> None:
        self._db.close()


# ---- registration ---------------------------------------------------------------------------


class SqliteVecConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str | None = Field(default=None, description="Database file (default <data dir>/vectors.db)")
    prefix: str = Field(default="", description="Prefix prepended to every collection (table) name")

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        if not _PREFIX_RE.match(v):
            raise ValueError("prefix may contain only lowercase letters, digits and '_' (at most 64 characters)")
        return v


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> SqliteVecStore:
    assert isinstance(config, SqliteVecConfig)
    path = Path(config.path) if config.path else data_dir / "vectors.db"
    return SqliteVecStore(path, prefix=config.prefix)


STORE_TYPE = VectorStoreType(
    type="sqlite_vec",
    label="SQLite (sqlite-vec)",
    description="Zero-setup local store; exact search, best under ~100k vectors.",
    config_model=SqliteVecConfig,
    secret_keys=(),
    capabilities=SqliteVecStore.capabilities,
    factory=_factory,
)
