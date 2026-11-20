"""Collection catalogs: where an adapter records what a collection was built with.

Every adapter must be able to answer "what dimension, metric, filter fields and index options does
this collection have?" so that ``ensure_collection`` can raise ``CollectionMismatch`` and
``collection_info`` can report the truth. Some databases have a place for that (an index mapping's
``_meta``, a collection description, a side collection, a key); some have none. This module gives
both cases one implementation of the rules, so adapters only supply storage:

* ``InStoreCatalog``: the adapter supplies ``read`` and ``write`` callables that keep one JSON
  string per collection inside the database itself.
* ``LocalCatalog``: a small SQLite file at ``<data dir>/store_catalogs/<type>.db`` keyed by
  ``(instance key, collection)``. Use it only when the store has nowhere to put the data (Pinecone).
  If the file is lost the collections are orphaned and rebuildable, like losing the core database.

Both implement ``Catalog``. ``ensure`` creates the entry, or checks the existing one against the
requested spec and merges new filter fields, raising ``CollectionMismatch`` on any other difference.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ragcentre.db.connection import Database
from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import FieldType
from ragcentre.stores.base import CollectionSpec, Metric, StoreError

CATALOG_DIR = "store_catalogs"
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


@dataclass(frozen=True)
class CatalogEntry:
    dim: int
    metric: Metric
    fields: dict[str, FieldType] = field(default_factory=dict)
    # Index-shaping options the collection was built with (immutable provider fields); compared
    # for equality by ``ensure``. Values must be JSON-serialisable.
    options: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"dim": self.dim, "metric": self.metric, "fields": self.fields, "options": self.options},
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> CatalogEntry:
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("entry must be an object")
            dim, metric = raw["dim"], raw["metric"]
            fields, options = raw.get("fields", {}), raw.get("options", {})
            if type(dim) is not int or dim < 1 or metric not in ("cosine", "l2", "dot"):
                raise ValueError("invalid dimension or metric")
            if not isinstance(fields, dict) or any(v not in ("str", "int", "float", "bool") for v in fields.values()):
                raise ValueError("invalid field types")
            if not isinstance(options, dict):
                raise ValueError("options must be an object")
            return cls(dim=dim, metric=metric, fields=fields, options=options)
        except (ValueError, KeyError, TypeError) as exc:
            raise StoreError(f"unreadable collection catalog entry: {exc}") from exc


@runtime_checkable
class Catalog(Protocol):
    def get(self, name: str) -> CatalogEntry | None:
        """The entry for ``name``, or ``None`` if the collection was never recorded."""
        ...

    def ensure(self, spec: CollectionSpec, options: Mapping[str, Any] | None = None) -> CatalogEntry:
        """Record ``spec`` if new, else check it; return the resulting entry.

        Raises ``CollectionMismatch`` if the stored dimension, metric, a field's type, or the index
        options differ. New filter fields are added to the entry. Safe to call concurrently.
        """
        ...

    def drop(self, name: str) -> None:
        """Forget ``name``. Idempotent."""
        ...


def _options(options: Mapping[str, Any] | None) -> dict[str, Any]:
    # Normalise through JSON so tuples/lists and key order compare equal to what was stored.
    return dict(json.loads(json.dumps(dict(options or {}), sort_keys=True)))


def reconcile(existing: CatalogEntry | None, spec: CollectionSpec, options: Mapping[str, Any] | None) -> CatalogEntry:
    """The rules of ``ensure_collection``, shared by every catalog. Pure: no storage access.

    Returns the entry that should be stored (``existing`` itself when nothing changes).
    """
    wanted = _options(options)
    if existing is None:
        return CatalogEntry(spec.dim, spec.metric, dict(spec.fields), wanted)
    if existing.dim != spec.dim or existing.metric != spec.metric:
        raise CollectionMismatch(
            f"collection {spec.name} exists with dim={existing.dim}, metric={existing.metric}; "
            f"requested dim={spec.dim}, metric={spec.metric}"
        )
    if existing.options != wanted:
        raise CollectionMismatch(
            f"collection {spec.name} was built with options {json.dumps(existing.options, sort_keys=True)}; "
            f"the store is now configured with {json.dumps(wanted, sort_keys=True)}. "
            "Index options cannot change on an existing collection: create a new store provider."
        )
    for fname, ftype in spec.fields.items():
        if fname in existing.fields and existing.fields[fname] != ftype:
            raise CollectionMismatch(f"field {fname} is {existing.fields[fname]} in {spec.name}, not {ftype}")
    merged = {**existing.fields, **spec.fields}
    if merged == existing.fields:
        return existing
    return CatalogEntry(existing.dim, existing.metric, merged, existing.options)


class InStoreCatalog:
    """A catalog kept inside the database, through callables the adapter supplies.

    ``read(name)`` returns the JSON string last passed to ``write`` for that collection, or
    ``None``. ``write(name, text)`` stores it (create or replace). ``remove(name)`` is optional:
    leave it out when the text lives on the collection itself and disappears with it.

    Calls through one catalog object are serialised. For separate objects or processes sharing
    storage, supply ``transaction``: a context-manager factory that locks the whole read/check/
    write sequence (a shared lock for threads, or a database transaction/advisory lock for
    processes). Callbacks must use that transaction and provide read-after-write visibility.
    Two ordinary read/write callbacks alone cannot make a distributed read-modify-write atomic.
    """

    def __init__(
        self,
        read: Callable[[str], str | None],
        write: Callable[[str, str], None],
        remove: Callable[[str], None] | None = None,
        *,
        transaction: Callable[[], AbstractContextManager[Any]] = nullcontext,
    ) -> None:
        self._read = read
        self._write = write
        self._remove = remove
        self._transaction = transaction
        self._lock = threading.Lock()

    def get(self, name: str) -> CatalogEntry | None:
        text = self._read(name)
        return None if text is None else CatalogEntry.from_json(text)

    def ensure(self, spec: CollectionSpec, options: Mapping[str, Any] | None = None) -> CatalogEntry:
        with self._lock, self._transaction():
            existing = self.get(spec.name)
            entry = reconcile(existing, spec, options)
            if entry is existing:
                assert existing is not None
                return existing
            self._write(spec.name, entry.to_json())
            stored = self.get(spec.name)
            if stored is None:
                raise StoreError(f"catalog write for {spec.name} was not visible on read-back")
            checked = reconcile(stored, spec, options)
            if checked != stored:
                raise StoreError(f"catalog write for {spec.name} did not retain the requested fields")
            return stored

    def drop(self, name: str) -> None:
        with self._lock, self._transaction():
            if self._remove is not None:
                self._remove(name)


class LocalCatalog:
    """A catalog in ``<data_dir>/store_catalogs/<store_type>.db``, keyed by instance and collection.

    ``instance_key`` tells apart two stores of one type that share the file (two providers, or two
    indexes of one server); build it from whatever identifies the physical target, for example
    ``f"{host}/{index_name}"``. Never put secrets in it.

    ``ensure`` is one ``BEGIN IMMEDIATE`` transaction, so concurrent callers in any number of
    threads or processes get exactly one winner and the others are checked against it.
    """

    def __init__(self, data_dir: Path | str, store_type: str, instance_key: str) -> None:
        if not _TYPE_RE.match(store_type):
            raise ValueError(f"invalid store type {store_type!r} for a catalog file name")
        self.instance_key = instance_key
        self.path = Path(data_dir) / CATALOG_DIR / f"{store_type}.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = Database(self.path)
        with self._db.write() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS catalog ("
                " instance_key TEXT NOT NULL, collection TEXT NOT NULL, entry TEXT NOT NULL,"
                " PRIMARY KEY (instance_key, collection))"
            )

    def get(self, name: str) -> CatalogEntry | None:
        return self.get_for_instance(name, self.instance_key)

    def get_for_instance(self, name: str, instance_key: str) -> CatalogEntry | None:
        """Read another identity without changing this catalog's active namespace.

        Adapters use this to detect legacy identities and report an explicit recovery path.
        """
        text = self._db.scalar(
            "SELECT entry FROM catalog WHERE instance_key = ? AND collection = ?", (instance_key, name)
        )
        return None if text is None else CatalogEntry.from_json(text)

    def ensure(self, spec: CollectionSpec, options: Mapping[str, Any] | None = None) -> CatalogEntry:
        with self._db.write() as conn:
            row = conn.execute(
                "SELECT entry FROM catalog WHERE instance_key = ? AND collection = ?", (self.instance_key, spec.name)
            ).fetchone()
            existing = None if row is None else CatalogEntry.from_json(row[0])
            entry = reconcile(existing, spec, options)
            if entry is not existing:
                conn.execute(
                    "INSERT OR REPLACE INTO catalog (instance_key, collection, entry) VALUES (?, ?, ?)",
                    (self.instance_key, spec.name, entry.to_json()),
                )
            return entry

    def drop(self, name: str) -> None:
        with self._db.write() as conn:
            conn.execute("DELETE FROM catalog WHERE instance_key = ? AND collection = ?", (self.instance_key, name))

    def close(self) -> None:
        self._db.close()
