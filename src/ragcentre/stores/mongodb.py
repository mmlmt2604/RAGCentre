"""MongoDB vector store (optional extra ``ragcentre[mongodb]``): Atlas Vector Search with ``$vectorSearch``.

Works on MongoDB Atlas, on ``mongodb/mongodb-atlas-local``, and on MongoDB Community 8.2+ with
``mongot`` (any deployment where ``createSearchIndex`` and ``$vectorSearch`` exist). A plain ``mongod``
without the search process fails ``healthcheck`` with a clear message.

Layout. One MongoDB collection per RAGCentre collection, named ``<prefix><collection>``. Each document
is ``{_id: <record id>, embedding: [floats], document_id: ..., <one key per schema field>}``. Each
collection has one ``vectorSearch`` search index (``rc_vector``) over ``embedding`` (``numDimensions``,
``similarity`` cosine or euclidean) with a ``filter`` entry for ``document_id`` and every schema field.
``ensure_collection`` creates the collection and the index and then *waits* until the index is queryable
and has built its latest definition. Adding a schema field updates the index definition (a rebuild; the
old index stays queryable meanwhile, and ``ensure_collection`` waits for the new one).

Catalog. A search index cannot say whether a filter field is an ``int`` or a ``float``, so the dimension,
metric and field types are also kept in a small side collection ``_ragcentre_collections`` of the same
database (``InStoreCatalog``; one ``{_id: <physical name>, entry: <JSON>}`` document each). The same
collection holds short-lived ``lock:<name>`` lease documents that serialise ``ensure_collection`` and
``drop_collection`` across threads, processes and machines. ``collection_info`` reports the dimension
and metric of the *index* (what queries run against), so an index that disagrees with the catalog
raises ``CollectionMismatch`` on the next ``ensure_collection`` instead of returning wrong scores.

Query. ``$vectorSearch`` with ``filter`` (a pre-filter, so nothing is filtered after the search),
``limit=k`` and, in the default approximate mode, ``numCandidates = min(10000, k * num_candidates_factor)``.
With ``exact=true`` the stage runs exact nearest neighbour (``exact: true``, no ``numCandidates``). A
query with ``k`` above 10000 (the server's ``numCandidates`` cap) is run exact. The filter operators
``$eq $ne $gt $gte $lt $lte $in $nin $and $or`` map one to one to MQL; ``$ne`` and ``$nin`` also match a
document that lacks the field, exactly as ``models.filters.matches`` does (verified).

Scores. Verified against MongoDB 9.0.8 (Atlas Local) and reproduced numerically (see DECISIONS):
``cosine`` scores are ``(1 + cos) / 2`` so the adapter returns ``2s - 1``; ``euclidean`` scores are
``1 / (1 + d**2)`` (*squared* distance) so it returns ``-sqrt(1/s - 1)``. ``dotProduct`` scores are
``(1 + dot) / 2`` clamped at 0 (a dot below -1 scores 0 and cannot be recovered), so ``dot`` is not
offered. Because the score is a float32 near 1.0 for near-identical vectors, the Euclidean distance
inverted from it carries an error of up to about 2.5e-4 close to 0 (ranking is unaffected).

Known limits, all measured on the local image: string values of 8191 characters or more are not matched
reliably by ``$eq``/``$in`` (the server truncates or drops them), so the store rejects strings over 8000
characters; a zero vector in a cosine collection is silently left out of the index and a zero query
vector is a server error, so the store rejects both; integers beyond 2**53 lose precision in filters.
Writes become visible to ``$vectorSearch`` a moment after they return (``consistency="eventual"``);
``collection_info(...).count`` is the number of documents queries can see now (the index's document
count, confirmed by an exact search for collections up to 2000 documents), so polling it waits for
visibility, as ``ragcentre store check`` does.

This module is named ``mongodb`` inside the ``ragcentre.stores`` package and the client library is
``pymongo``, so ``import pymongo`` here is always the third-party client (it is imported lazily).
"""

from __future__ import annotations

import contextlib
import math
import re
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import And, FieldType, Filter, Or, type_ok
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
from ragcentre.stores.filter_sql import FIELD_RE
from ragcentre.stores.registry import VectorStoreType

VECTOR_FIELD = "embedding"
INDEX_NAME = "rc_vector"
CATALOG_COLLECTION = "_ragcentre_collections"
LOCK_ID_PREFIX = "lock:"
LAYOUT_VERSION = 1  # recorded in the catalog; a different document layout would be a different index
MAX_DIM = 8192  # numDimensions must be within [1..8192] (verified on the server)
MAX_NUM_CANDIDATES = 10000  # "numCandidates" must be within [1..10000] (verified)
VISIBLE_PROBE_LIMIT = 2000  # collections up to this size are counted by an exact search
MAX_STRING_CHARS = 8000  # longer strings are not matched reliably by filters (verified; see module doc)
DEFAULT_NUM_CANDIDATES_FACTOR = 20
INDEX_WAIT_SECONDS = 600.0
LOCK_LEASE_SECONDS = 900.0
RESERVED_FIELDS = frozenset({"_id", VECTOR_FIELD})
_SIMILARITY: dict[Metric, str] = {"cosine": "cosine", "l2": "euclidean"}
_METRIC_FROM_SIMILARITY: dict[str, Metric] = {v: k for k, v in _SIMILARITY.items()}
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,38}$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]{0,32}$")
_SCHEMES = ("mongodb://", "mongodb+srv://")
_INT_LIMIT = 2**53  # search indexes compare numeric filters at double precision
_DUPLICATE_KEY = 11000
_INDEX_EXISTS = 68  # "Index <name> already exists with a different definition"


def _import() -> Any:
    return require_extra("pymongo", store="mongodb", extra="mongodb", package="pymongo")


# ---- the filter compiler --------------------------------------------------------------------


def _scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -_INT_LIMIT <= value <= _INT_LIMIT:
            raise StoreError("filter integers must be within +/-2**53 for exact numeric comparisons")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StoreError("filter values must be finite numbers")
        return value
    if isinstance(value, str):
        return value
    raise StoreError(f"unsupported filter value of type {type(value).__name__}")


def compile_filter(f: Filter, fields: Mapping[str, FieldType]) -> dict[str, Any]:
    """Compile a parsed filter to the MQL expression ``$vectorSearch`` accepts in ``filter``.

    Every operator maps to the MQL operator of the same name, and ``$and``/``$or`` to the same
    combinators, so no value is ever interpolated and nothing is evaluated after the search. Field
    names must be declared filter fields of the collection (a name that starts with ``$`` or contains
    ``.`` could address another field or an operator, and is rejected). ``$ne`` and ``$nin`` match a
    document without the field, as in ``models.filters.matches``.
    """
    if isinstance(f, (And, Or)):
        return {("$and" if isinstance(f, And) else "$or"): [compile_filter(item, fields) for item in f.items]}
    name = f.field
    if not FIELD_RE.fullmatch(name):
        raise StoreError(f"invalid filter field name {name!r}")
    ftype = fields.get(name)
    if ftype is None:
        raise StoreError(f"field {name!r} is not a filterable field of this collection")
    value: Any
    if isinstance(f.value, tuple):
        if f.op not in ("$in", "$nin"):
            raise StoreError(f"operator {f.op} takes a single value")
        values = [_scalar(v) for v in f.value]
        if not all(type_ok(v, ftype) for v in values):
            raise StoreError(f"expected {ftype} values for field {name}")
        value = values
    else:
        if f.op in ("$in", "$nin"):
            raise StoreError(f"operator {f.op} takes a list")
        value = _scalar(f.value)
        if not type_ok(value, ftype):
            raise StoreError(f"expected a {ftype} value for field {name}")
    if f.op in ("$gt", "$gte", "$lt", "$lte") and ftype not in ("int", "float"):
        raise StoreError(f"operator {f.op} does not apply to {ftype} field {name}")
    if f.op not in ("$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin"):
        raise StoreError(f"unsupported filter operator {f.op}")
    return {name: {f.op: value}}


def to_score(metric: Metric, raw: float) -> float:
    """``vectorSearchScore`` back to the plan's score (higher is better).

    cosine: score ``s = (1 + cos) / 2``, so ``cos = 2s - 1``. l2: ``s = 1 / (1 + d**2)``, so
    ``-d = -sqrt(1/s - 1)``.
    """
    if metric == "cosine":
        return max(-1.0, min(1.0, 2.0 * raw - 1.0))
    return -math.sqrt(max(1.0 / max(raw, 1e-12) - 1.0, 0.0))


# ---- search-index helpers (pure) ------------------------------------------------------------


def index_definition(dim: int, metric: Metric, filter_paths: Iterable[str]) -> dict[str, Any]:
    return {
        "fields": [
            {"type": "vector", "path": VECTOR_FIELD, "numDimensions": dim, "similarity": _SIMILARITY[metric]},
            *({"type": "filter", "path": p} for p in sorted(set(filter_paths))),
        ]
    }


def parse_index_definition(definition: Mapping[str, Any]) -> tuple[int, str, set[str]]:
    """``(numDimensions, similarity, filter paths)`` of an index definition as the server lists it."""
    dim, similarity, paths = 0, "", set()
    for entry in definition.get("fields", []):
        if entry.get("type") == "vector" and entry.get("path") == VECTOR_FIELD:
            dim, similarity = int(entry.get("numDimensions", 0)), str(entry.get("similarity", ""))
        elif entry.get("type") == "filter":
            paths.add(str(entry.get("path")))
    return dim, similarity, paths


def index_state(idx: Mapping[str, Any]) -> Literal["ready", "building", "failed"]:
    """Whether a ``listSearchIndexes`` row is fully built for its latest definition.

    The server keeps an old index queryable while a new definition builds, so ``queryable`` alone is not
    enough: the status must be READY and every host's main index must be at the latest definition version.
    """
    status = idx.get("status")
    if status == "FAILED":
        return "failed"
    if status != "READY" or not idx.get("queryable"):
        return "building"
    latest = (idx.get("latestDefinitionVersion") or {}).get("version")
    for detail in idx.get("statusDetail") or []:
        main = (detail.get("mainIndex") or {}).get("definitionVersion", {}).get("version")
        if latest is not None and main is not None and main != latest:
            return "building"
    return "ready"


# The type's listing shows the default configuration (approximate search); an instance with
# ``exact=True`` reports ``filter_exact=True``.
CAPABILITIES = Capabilities(
    metrics=frozenset({"cosine", "l2"}),
    max_batch=500,
    max_dim=MAX_DIM,
    consistency="eventual",
    filter_exact=False,
)


# ---- store ----------------------------------------------------------------------------------


class MongoDBStore:
    def __init__(
        self,
        client: Any,
        *,
        database: str = "ragcentre",
        exact: bool = False,
        num_candidates_factor: int = DEFAULT_NUM_CANDIDATES_FACTOR,
        prefix: str = "",
        scrub: str | None = None,
        index_wait: float = INDEX_WAIT_SECONDS,
    ) -> None:
        self._pm = _import()
        self._client = client
        self._db: Any = client[database]
        self._exact = exact
        self._factor = num_candidates_factor
        self._prefix = prefix
        self._scrub = scrub  # the connection string: removed from any message that could carry it
        self._index_wait = index_wait
        self.capabilities = replace(CAPABILITIES, filter_exact=exact)
        self._meta: Any = self._db[CATALOG_COLLECTION]
        self._catalog = InStoreCatalog(self._catalog_read, self._catalog_write, self._catalog_remove)

    @classmethod
    def connect(
        cls,
        *,
        uri: str,
        database: str = "ragcentre",
        exact: bool = False,
        num_candidates_factor: int = DEFAULT_NUM_CANDIDATES_FACTOR,
        prefix: str = "",
    ) -> MongoDBStore:
        pymongo = _import()
        if not uri.startswith(_SCHEMES):
            raise StoreError("mongodb: the 'uri' secret must start with mongodb:// or mongodb+srv://")
        try:
            client = pymongo.MongoClient(
                uri,
                appname="ragcentre",
                serverSelectionTimeoutMS=15000,
                connect=False,
                retryReads=False,
            )
        except Exception as exc:  # the uri may hold a password: report the kind of problem only
            raise StoreError(f"mongodb: invalid connection string ({type(exc).__name__})") from None
        return cls(
            client,
            database=database,
            exact=exact,
            num_candidates_factor=num_candidates_factor,
            prefix=prefix,
            scrub=uri,
        )

    # ---- helpers ------------------------------------------------------------------------------

    def _clean(self, text: str) -> str:
        return text.replace(self._scrub, "<uri>") if self._scrub else text

    @contextlib.contextmanager
    def _guard(self, what: str) -> Iterator[None]:
        """Turn client errors into ``StoreError`` (never leaking the connection string)."""
        try:
            yield
        except (StoreError, CollectionMismatch):
            raise
        except self._pm.errors.PyMongoError as exc:
            hint = ""
            if isinstance(exc, self._pm.errors.OperationFailure) and exc.code in (13, 18):
                hint = " (check the user, password and permissions in the 'uri' secret)"
            raise StoreError(
                f"mongodb: {what} failed ({type(exc).__name__}): {self._clean(str(exc))[:500]}{hint}"
            ) from None

    def _physical(self, name: str) -> str:
        check_collection_name(name)
        physical = self._prefix + name
        if physical == CATALOG_COLLECTION:
            raise StoreError(f"collection name {physical!r} is reserved")
        return physical

    # The catalog is keyed by logical collection name; its documents are keyed by physical name.
    def _catalog_read(self, name: str) -> str | None:
        doc = self._meta.find_one({"_id": self._physical(name)})
        return None if doc is None else str(doc["entry"])

    def _catalog_write(self, name: str, text: str) -> None:
        physical = self._physical(name)
        self._meta.replace_one({"_id": physical}, {"_id": physical, "entry": text}, upsert=True)

    def _catalog_remove(self, name: str) -> None:
        self._meta.delete_one({"_id": self._physical(name)})

    @staticmethod
    def _options() -> dict[str, Any]:
        return {"layout": LAYOUT_VERSION, "vector_field": VECTOR_FIELD}

    def _index(self, physical: str) -> dict[str, Any] | None:
        for idx in self._db[physical].list_search_indexes(INDEX_NAME):
            return dict(idx)
        return None

    @contextlib.contextmanager
    def _lock(self, physical: str) -> Iterator[None]:
        """A lease in the catalog collection: one holder at a time across threads and processes."""
        key, token = f"{LOCK_ID_PREFIX}{physical}", uuid.uuid4().hex
        deadline = time.monotonic() + self._index_wait + LOCK_LEASE_SECONDS
        while True:
            now = time.time()
            try:
                self._meta.update_one(
                    {"_id": key, "expires": {"$lt": now}},
                    {"$set": {"owner": token, "expires": now + LOCK_LEASE_SECONDS}},
                    upsert=True,
                )
                break
            except self._pm.errors.DuplicateKeyError:  # held, and not expired
                if time.monotonic() >= deadline:
                    raise StoreError(
                        f"mongodb: timed out waiting for another worker to finish with {physical}"
                    ) from None
                time.sleep(0.2)
        try:
            yield
        finally:
            with contextlib.suppress(self._pm.errors.PyMongoError):
                self._meta.delete_one({"_id": key, "owner": token})

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        with self._guard("health check"):
            self._client.admin.command("ping")
            try:
                list(self._meta.list_search_indexes())
            except self._pm.errors.OperationFailure as exc:
                if exc.code in (13, 18):
                    raise
                raise StoreError(
                    "mongodb: the server is reachable but does not support search indexes "
                    "(Atlas Vector Search needs MongoDB Atlas, Atlas Local, or Community with mongot): "
                    f"{self._clean(str(exc))[:200]}"
                ) from None

    def _prepare(self, spec: CollectionSpec) -> tuple[str, CollectionSpec]:
        check_spec(spec, self.capabilities)
        physical = self._physical(spec.name)
        fields: dict[str, FieldType] = {**spec.fields, "document_id": "str"}
        for fname in fields:
            if fname in RESERVED_FIELDS or not FIELD_RE.fullmatch(fname):
                raise StoreError(f"field name {fname!r} is reserved or not a valid MongoDB field name")
        return physical, CollectionSpec(spec.name, spec.dim, spec.metric, fields)

    def ensure_collection(self, spec: CollectionSpec) -> None:
        physical, spec = self._prepare(spec)
        with self._guard("ensure_collection"), self._lock(physical):
            entry = self._catalog.ensure(spec, self._options())  # raises CollectionMismatch
            with contextlib.suppress(self._pm.errors.CollectionInvalid):
                self._db.create_collection(physical)
            self._ensure_index(physical, entry)

    def _ensure_index(self, physical: str, entry: CatalogEntry) -> None:
        wanted = set(entry.fields)
        deadline = time.monotonic() + self._index_wait
        while True:
            idx = self._index(physical)
            if idx is not None and idx.get("status") != "DELETING":
                dim, similarity, paths = parse_index_definition(idx.get("latestDefinition") or {})
                if dim != entry.dim or _METRIC_FROM_SIMILARITY.get(similarity) != entry.metric:
                    raise CollectionMismatch(
                        f"collection {physical} has a search index with dimension {dim} and similarity "
                        f"{similarity!r}; requested {entry.dim} and {_SIMILARITY[entry.metric]!r}"
                    )
                if not wanted <= paths:  # a field was added: update the definition (a rebuild)
                    self._db[physical].update_search_index(
                        INDEX_NAME, index_definition(entry.dim, entry.metric, wanted | paths)
                    )
                break
            if idx is None:
                try:
                    self._db[physical].create_search_index(
                        self._pm.operations.SearchIndexModel(
                            definition=index_definition(entry.dim, entry.metric, wanted),
                            name=INDEX_NAME,
                            type="vectorSearch",
                        )
                    )
                    break
                except self._pm.errors.OperationFailure as exc:
                    if exc.code != _INDEX_EXISTS:
                        raise
            # DELETING (a dropped collection's index is going away) or a create that raced: try again
            if time.monotonic() >= deadline:
                raise StoreError(f"mongodb: the old search index of {physical} did not go away in time")
            time.sleep(0.5)
        self._wait_ready(physical)

    def _wait_ready(self, physical: str) -> None:
        deadline = time.monotonic() + self._index_wait
        while True:
            idx = self._index(physical)
            state = "building" if idx is None else index_state(idx)
            if state == "ready":
                return
            if state == "failed":
                assert idx is not None
                raise StoreError(f"mongodb: building the search index of {physical} failed: {str(idx)[:300]}")
            if time.monotonic() >= deadline:
                raise StoreError(f"mongodb: the search index of {physical} was not ready after {self._index_wait:.0f}s")
            time.sleep(0.25)

    def _visible(self, physical: str, dim: int, limit: int) -> int:
        """How many documents an exact search returns (at most ``limit``): what is searchable now."""
        stage = {
            "index": INDEX_NAME,
            "path": VECTOR_FIELD,
            "queryVector": [1.0] + [0.0] * (dim - 1),
            "exact": True,
            "limit": limit,
        }
        try:
            return sum(1 for _ in self._db[physical].aggregate([{"$vectorSearch": stage}, {"$project": {"_id": 1}}]))
        except self._pm.errors.PyMongoError:
            return limit

    def collection_info(self, name: str) -> CollectionInfo | None:
        physical = self._physical(name)
        with self._guard("collection_info"):
            if self._catalog.get(name) is None:
                return None
            idx = self._index(physical)
            if idx is None or idx.get("status") == "DELETING":
                return None
            dim, similarity, _ = parse_index_definition(idx.get("latestDefinition") or {})
            metric = _METRIC_FROM_SIMILARITY.get(similarity)
            if metric is None or dim < 1:
                raise StoreError(f"mongodb: the search index of {physical} is not a RAGCentre index")
            # The count is what queries can see, so a caller that waits for visibility can poll it. The
            # index's own document count (``numDocs``) can run ahead of the searcher by a few seconds
            # (measured), so a small collection is also counted by an exact search (``_visible``).
            indexed = idx.get("numDocs")
            if not isinstance(indexed, int):
                indexed = self._db[physical].estimated_document_count()
            count = (
                min(indexed, self._visible(physical, dim, indexed)) if 0 < indexed <= VISIBLE_PROBE_LIMIT else indexed
            )
            return CollectionInfo(dim, metric, int(count))

    def drop_collection(self, name: str) -> None:
        physical = self._physical(name)
        with self._guard("drop_collection"), self._lock(physical):
            # Collection first: a crash in between leaves a catalog entry that ensure_collection repairs,
            # never an old collection whose records would reappear under a new catalog entry.
            self._db.drop_collection(physical)  # also drops its search index
            self._catalog.drop(name)

    def _entry(self, name: str) -> CatalogEntry:
        entry = self._catalog.get(name)
        if entry is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        return entry

    def _document(self, rec: Record, entry: CatalogEntry) -> dict[str, Any]:
        if len(rec.vector) != entry.dim:
            raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection has {entry.dim}")
        _check_vector(rec.vector, entry.metric, f"record {rec.id}")
        doc: dict[str, Any] = {"_id": rec.id, VECTOR_FIELD: [float(x) for x in rec.vector]}
        for key, value in rec.metadata.items():
            ftype = entry.fields.get(key)
            if ftype is None:  # not a declared filter field (the collection may predate it): not stored
                continue
            if not type_ok(value, ftype):
                raise StoreError(f"record {rec.id}: field {key} must be {ftype}, got {type(value).__name__}")
            if isinstance(value, str) and len(value) > MAX_STRING_CHARS:
                raise StoreError(
                    f"record {rec.id}: field {key} is {len(value)} characters; filter fields over "
                    f"{MAX_STRING_CHARS} are not matched reliably by MongoDB search indexes"
                )
            if isinstance(value, int) and not isinstance(value, bool) and not -_INT_LIMIT <= value <= _INT_LIMIT:
                raise StoreError(f"record {rec.id}: field {key} must be within +/-2**53 for exact numeric comparisons")
            doc[key] = value
        return doc

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        physical = self._physical(name)
        with self._guard("upsert"):
            if not records:
                return
            entry = self._entry(name)
            docs = {rec.id: self._document(rec, entry) for rec in records}  # a repeated id: the last wins
            ops = [self._pm.ReplaceOne({"_id": rid}, doc, upsert=True) for rid, doc in docs.items()]
            for attempt in (1, 2):
                try:
                    self._db[physical].bulk_write(ops, ordered=False)
                    return
                except self._pm.errors.BulkWriteError as exc:
                    # Two writers inserting the same new _id at once: one loses the race; retry once.
                    errors = exc.details.get("writeErrors", [])
                    if attempt == 2 or not errors or any(e.get("code") != _DUPLICATE_KEY for e in errors):
                        raise

    def delete(self, name: str, ids: Sequence[str]) -> None:
        physical = self._physical(name)
        if not ids:
            return
        with self._guard("delete"):
            self._db[physical].delete_many({"_id": {"$in": list(ids)}})

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        physical = self._physical(name)
        with self._guard("query"):
            entry = self._entry(name)
            if len(vector) != entry.dim:
                raise StoreError(f"query has dimension {len(vector)}; collection {name} has {entry.dim}")
            _check_vector(vector, entry.metric, "query vector")
            if k < 1:
                return []
            stage: dict[str, Any] = {
                "index": INDEX_NAME,
                "path": VECTOR_FIELD,
                "queryVector": [float(x) for x in vector],
                "limit": k,
            }
            if self._exact or k > MAX_NUM_CANDIDATES:  # numCandidates cannot reach k: search exactly
                stage["exact"] = True
            else:
                stage["numCandidates"] = min(MAX_NUM_CANDIDATES, max(k, k * self._factor))
            if filter is not None:
                stage["filter"] = compile_filter(filter, entry.fields)
            rows = self._db[physical].aggregate(
                [{"$vectorSearch": stage}, {"$project": {"_id": 1, "s": {"$meta": "vectorSearchScore"}}}]
            )
            matches = [Match(id=str(row["_id"]), score=to_score(entry.metric, float(row["s"]))) for row in rows]
        matches.sort(key=lambda m: -m.score)
        return matches[:k]

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()


def _check_vector(vector: Sequence[float], metric: Metric, what: str) -> None:
    if not all(math.isfinite(x) for x in vector):
        raise StoreError(f"{what} contains a NaN or infinite value")
    if metric == "cosine" and not any(vector):
        raise StoreError(f"{what} is a zero vector; MongoDB cannot index or search one with cosine similarity")


# ---- configuration and registration ---------------------------------------------------------


class MongoDBConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str = Field(
        default="ragcentre", description="Database that holds the collections (letters, digits, _ and -)"
    )
    exact: bool = Field(
        default=False,
        description=(
            "Run exact nearest-neighbour search (ENN): every filtered query returns min(k, matches) results, "
            "slower on large collections. Off: approximate search with numCandidates"
        ),
    )
    num_candidates_factor: int = Field(
        default=DEFAULT_NUM_CANDIDATES_FACTOR,
        ge=1,
        le=1000,
        description="Approximate search: numCandidates = min(10000, k x factor). Higher is slower and more accurate",
    )
    prefix: str = Field(default="", description="Prefix of every collection this store creates")

    @field_validator("database")
    @classmethod
    def _check_database(cls, v: str) -> str:
        if not _NAME_RE.fullmatch(v):
            raise ValueError("database may contain only letters, digits, '_' and '-' (at most 38 characters)")
        return v

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        if not _PREFIX_RE.fullmatch(v):
            raise ValueError("prefix may contain only letters, digits, '_' and '-' (at most 32 characters)")
        return v


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> MongoDBStore:
    assert isinstance(config, MongoDBConfig)
    uri = secrets.get("uri")
    if not uri:
        raise StoreError("mongodb: the 'uri' secret (a MongoDB connection string) is required")
    return MongoDBStore.connect(
        uri=uri,
        database=config.database,
        exact=config.exact,
        num_candidates_factor=config.num_candidates_factor,
        prefix=config.prefix,
    )


STORE_TYPE = VectorStoreType(
    type="mongodb",
    label="MongoDB",
    description=(
        "MongoDB Atlas Vector Search ($vectorSearch), also Atlas Local and Community with search: "
        "a collection and search index per collection. Needs the optional 'mongodb' extra."
    ),
    config_model=MongoDBConfig,
    secret_keys=("uri",),
    capabilities=CAPABILITIES,
    factory=_factory,
)
