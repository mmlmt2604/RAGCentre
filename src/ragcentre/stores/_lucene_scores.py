"""Code shared by the Elasticsearch and OpenSearch stores: score inverses, the filter compiler and
``SearchEngineStore``, the adapter logic the two have in common (the engines differ only in client
calls, the vector mapping and the k-NN query body).

**Scores** (plan section 4.1). Lucene-based k-NN returns a *transformed, non-negative* relevance
score instead of the similarity. Inverting it recovers RAGCentre's score:

==========  ==========================================  ====================================
metric      engine score ``s``                          RAGCentre score
==========  ==========================================  ====================================
cosine      ``(1 + cos) / 2``                           ``2s - 1``
dot         ``dot + 1`` if ``dot >= 0`` else            ``s - 1`` if ``s >= 1`` else
            ``1 / (1 - dot)``                           ``1 - 1/s``
l2          ``1 / (1 + d**2)`` (``d`` Euclidean)        ``-sqrt(1/s - 1)``
==========  ==========================================  ====================================

Float32 rounding of ``s`` near 1 costs precision for near-identical vectors (and for a dot
product near zero); ranking is unaffected. ``tests/test_vs_*`` record the verified behaviour.

**Filters.** Both engines take the same ``bool``/``term``/``terms``/``range`` query DSL, so one
compiler serves both. The DSL is JSON, so values need no escaping; what remains is the field name,
which is validated (``filter_sql.FIELD_RE`` with ``fullmatch``, plus an allow-list of the collection's
known fields) before it becomes a JSON key. Semantics follow ``models.filters.matches``: a missing
field satisfies only ``$ne`` and ``$nin``. ``must_not`` gives exactly that, because a document that
lacks the field has no ``term`` match to exclude. The compiled query is meant for the filter slot
*inside* the k-NN clause; a filter beside it would be applied after the top-k is chosen.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping, Sequence
from typing import Any, ClassVar

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
)
from ragcentre.stores.catalog import CatalogEntry, InStoreCatalog, reconcile
from ragcentre.stores.filter_sql import check_field

_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
_RANGE = {"$gt": "gt", "$gte": "gte", "$lt": "lt", "$lte": "lte"}

# ---- scores ---------------------------------------------------------------------------------


def invert_score(metric: Metric, score: float) -> float:
    """RAGCentre's score for a Lucene-transformed engine score ``score``."""
    if metric == "cosine":
        return 2.0 * score - 1.0
    if metric == "dot":
        return score - 1.0 if score >= 1.0 else 1.0 - 1.0 / score
    if score <= 0.0:  # cannot happen for 1/(1+d^2); guard the division anyway
        return -math.inf
    return -math.sqrt(max(1.0 / score - 1.0, 0.0))


def forward_score(metric: Metric, value: float) -> float:
    """The engine score for a RAGCentre score (the exact inverse of ``invert_score``); for tests."""
    if metric == "cosine":
        return (1.0 + value) / 2.0
    if metric == "dot":
        return value + 1.0 if value >= 0.0 else 1.0 / (1.0 - value)
    return 1.0 / (1.0 + value * value)


# ---- filters --------------------------------------------------------------------------------


def _value(value: Any) -> Any:
    """A filter value as a JSON scalar. Booleans stay booleans (ES/OpenSearch ``boolean`` fields)."""
    if isinstance(value, bool):  # before int: bool is an int subclass
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("string values must be valid Unicode (no lone surrogates)") from exc
        return value
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise ValueError(f"integer {value} does not fit in 64 bits")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("float values must be finite")
        return value
    raise ValueError(f"unsupported filter value type {type(value).__name__}")


def _field(name: str, allowed: Collection[str] | None) -> str:
    check_field(name)
    if allowed is not None and name not in allowed:
        raise ValueError(f"unknown field {name!r}")
    return name


def compile_query_dsl(f: Filter, allowed: Collection[str] | None = None) -> dict[str, Any]:
    """The ``bool``/``term``/``terms``/``range`` query for ``f``.

    ``allowed`` is the set of field names the collection has; any other field raises ``ValueError``,
    as does an invalid field name or value.
    """
    if isinstance(f, And):
        return {"bool": {"filter": [compile_query_dsl(i, allowed) for i in f.items]}}
    if isinstance(f, Or):
        return {"bool": {"should": [compile_query_dsl(i, allowed) for i in f.items], "minimum_should_match": 1}}
    assert isinstance(f, Cond)
    name = _field(f.field, allowed)
    op = f.op
    if op in ("$eq", "$ne"):
        assert not isinstance(f.value, tuple)
        term = {"term": {name: {"value": _value(f.value)}}}
        return term if op == "$eq" else {"bool": {"must_not": [term]}}
    if op in _RANGE:
        assert not isinstance(f.value, tuple)
        value = _value(f.value)
        if isinstance(value, (bool, str)):
            raise ValueError(f"{op} applies to numbers only")
        return {"range": {name: {_RANGE[op]: value}}}
    assert isinstance(f.value, tuple)
    terms = {"terms": {name: [_value(v) for v in f.value]}}
    return terms if op == "$in" else {"bool": {"must_not": [terms]}}


def compile_filter(f: Filter | None, allowed: Collection[str] | None = None) -> dict[str, Any] | None:
    """The filter query to place inside the k-NN clause, or ``None`` when there is no filter."""
    return None if f is None else compile_query_dsl(f, allowed)


# ---- the shared store -----------------------------------------------------------------------

META_KEY = "ragcentre"
VECTOR_FIELD = "_rc_vector"
RESERVED_PREFIX = "_rc_"
MAX_WINDOW = 10_000  # k-NN ``num_candidates`` and the default result window both stop here
INDEX_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_FIELD_MAPPING: dict[FieldType, str] = {"str": "keyword", "int": "long", "float": "double", "bool": "boolean"}


def check_index_prefix(prefix: str) -> str:
    """An index-name prefix: lowercase, starts with a letter or digit, no characters the engines forbid."""
    if prefix and not INDEX_PREFIX_RE.fullmatch(prefix):
        raise ValueError(
            "prefix must be lowercase, start with a letter or digit, and contain only a-z, 0-9, '_', '-' and '.'"
        )
    if len(prefix) > 100:
        raise ValueError("prefix must be at most 100 characters")
    return prefix


def error_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def error_type(exc: BaseException) -> str:
    """The server's error ``type`` (``index_not_found_exception`` ...) for an ES or OpenSearch client error."""
    kind = getattr(exc, "error", None)
    return kind if isinstance(kind, str) else ""


class SearchEngineStore(ABC):
    """One collection is one index named ``<prefix><collection>`` with a vector field, a ``document_id``
    keyword and a typed field per schema field. The catalog (dimension, metric, field types and the
    index-shaping options) is kept in the index mapping's ``_meta`` and is written by the same call
    that creates the index, so a collection never exists without it.

    Subclasses supply the client calls and the engine-specific mapping and query bodies.
    """

    label: ClassVar[str]  # "elasticsearch" | "opensearch", used in error messages
    capabilities: Capabilities

    def __init__(self, client: Any, *, prefix: str, num_candidates_factor: int, options: Mapping[str, Any]) -> None:
        self._client = client
        self._prefix = prefix
        self._factor = num_candidates_factor
        self._options = dict(options)
        self._catalog = InStoreCatalog(self._read_meta, self._write_meta)

    # ---- engine hooks -------------------------------------------------------------------------

    @abstractmethod
    def _info(self) -> dict[str, Any]: ...

    @abstractmethod
    def _get_mappings(self, index: str) -> dict[str, Any] | None:
        """The index's mapping (``{"_meta": ..., "properties": ...}``), or ``None`` if it does not exist."""

    @abstractmethod
    def _create_index(self, index: str, mappings: dict[str, Any]) -> None: ...

    @abstractmethod
    def _put_mappings(self, index: str, mappings: dict[str, Any]) -> None:
        """Add properties and replace ``_meta`` (``mappings`` holds only those two keys)."""

    @abstractmethod
    def _delete_index(self, index: str) -> None: ...

    @abstractmethod
    def _bulk(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """Run a bulk request and wait for the next refresh, so the writes are searchable on return."""

    @abstractmethod
    def _count(self, index: str) -> int: ...

    @abstractmethod
    def _knn(self, index: str, entry: CatalogEntry, vector: Sequence[float], k: int, flt: dict[str, Any] | None) -> Any:
        """The raw search response for a k-NN query with ``flt`` inside the k-NN clause."""

    @abstractmethod
    def _vector_mapping(self, dim: int, metric: Metric) -> dict[str, Any]: ...

    @abstractmethod
    def _check_server(self, entry_options: Mapping[str, Any], metric: Metric) -> None:
        """Raise ``StoreError`` if the server cannot build this collection (version, engine support)."""

    def _close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                close()

    # ---- helpers ------------------------------------------------------------------------------

    def _physical(self, name: str) -> str:
        check_collection_name(name)
        physical = self._prefix + name
        if len(physical.encode("utf-8")) > 255 or not INDEX_PREFIX_RE.fullmatch(physical):
            raise StoreError(
                f"{self.label}: {physical!r} (prefix {self._prefix!r} plus {name!r}) is not a valid index name"
            )
        return physical

    def _fail(self, what: str, name: str, exc: Exception) -> StoreError:
        if error_status(exc) == 404 or error_type(exc) == "index_not_found_exception":
            return CollectionNotFound(f"collection {name} does not exist")
        return StoreError(f"{self.label}: {what} on {name} failed: {exc}")

    def _read_meta(self, name: str) -> str | None:
        try:
            mappings = self._get_mappings(self._physical(name))
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"{self.label}: could not read collection {name}: {exc}") from exc
        if mappings is None:
            return None
        meta = (mappings.get("_meta") or {}).get(META_KEY)
        if meta is None or VECTOR_FIELD not in (mappings.get("properties") or {}):
            raise StoreError(f"{self.label} index for collection {name} was not created by RAGCentre")
        return json.dumps(meta)

    def _properties(self, fields: Mapping[str, FieldType]) -> dict[str, Any]:
        return {f: {"type": _FIELD_MAPPING[t]} for f, t in fields.items()}

    def _write_meta(self, name: str, text: str) -> None:
        """Create the index with its catalog, or add fields and replace the catalog of an existing one."""
        entry = CatalogEntry.from_json(text)
        index = self._physical(name)
        meta = {META_KEY: json.loads(text)}
        try:
            if self._get_mappings(index) is None:
                mappings = {
                    "dynamic": "strict",
                    "_meta": meta,
                    "properties": {
                        VECTOR_FIELD: self._vector_mapping(entry.dim, entry.metric),
                        **self._properties(entry.fields),
                    },
                }
                try:
                    self._create_index(index, mappings)
                    return
                except Exception as exc:
                    if error_type(exc) != "resource_already_exists_exception":
                        raise
                    # created concurrently (another process): check it against what we wanted below
                    existing_text = self._read_meta(name)
                    assert existing_text is not None
                    spec = CollectionSpec(name, entry.dim, entry.metric, dict(entry.fields))
                    merged = reconcile(CatalogEntry.from_json(existing_text), spec, entry.options)  # may raise
                    entry = merged
                    meta = {META_KEY: json.loads(merged.to_json())}
            self._put_mappings(index, {"properties": self._properties(entry.fields), "_meta": meta})
        except (StoreError, CollectionMismatch):
            raise
        except Exception as exc:
            raise StoreError(f"{self.label}: could not write collection {name}: {exc}") from exc

    def _entry(self, name: str) -> CatalogEntry:
        entry = self._catalog.get(name)
        if entry is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        return entry

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        try:
            self._info()
        except Exception as exc:
            raise StoreError(f"{self.label} is not reachable: {exc}") from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        bad = [f for f in spec.fields if f.startswith(RESERVED_PREFIX)]
        if bad:
            raise StoreError(
                f"field names starting with {RESERVED_PREFIX!r} are reserved by the {self.label} store: {bad}"
            )
        for fname in spec.fields:
            check_field(fname)
        spec = CollectionSpec(spec.name, spec.dim, spec.metric, {"document_id": "str", **spec.fields})
        try:
            self._check_server(self._options, spec.metric)
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"{self.label}: could not query the server: {exc}") from exc
        self._catalog.ensure(spec, self._options)

    def collection_info(self, name: str) -> CollectionInfo | None:
        check_collection_name(name)
        entry = self._catalog.get(name)
        if entry is None:
            return None
        try:
            count = self._count(self._physical(name))
        except Exception as exc:
            if error_status(exc) == 404:
                return None
            raise StoreError(f"{self.label}: count on {name} failed: {exc}") from exc
        return CollectionInfo(dim=entry.dim, metric=entry.metric, count=count)

    def drop_collection(self, name: str) -> None:
        check_collection_name(name)
        try:
            self._delete_index(self._physical(name))
        except Exception as exc:
            if error_status(exc) != 404:
                raise StoreError(f"{self.label}: could not drop collection {name}: {exc}") from exc

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        entry = self._entry(name)
        if not records:
            return
        index = self._physical(name)
        operations: list[dict[str, Any]] = []
        for rec in records:
            if len(rec.vector) != entry.dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {entry.dim}")
            if not all(math.isfinite(x) for x in rec.vector):
                raise StoreError(f"record {rec.id} has a non-finite vector component")
            if entry.metric == "cosine" and not any(rec.vector):
                raise StoreError(
                    f"record {rec.id} is a zero vector, which {self.label} cannot score with cosine similarity"
                )
            doc: dict[str, Any] = {VECTOR_FIELD: [float(x) for x in rec.vector]}
            for fname, ftype in entry.fields.items():  # keys outside the schema are not filterable: dropped
                value = rec.metadata.get(fname)
                if value is not None:
                    doc[fname] = float(value) if ftype == "float" else value
            operations.append({"index": {"_index": index, "_id": rec.id}})
            operations.append(doc)
        try:
            response = self._bulk(operations)
        except Exception as exc:
            raise self._fail("upsert", name, exc) from exc
        self._raise_item_errors(name, response)

    def delete(self, name: str, ids: Sequence[str]) -> None:
        check_collection_name(name)
        if not ids:
            return
        index = self._physical(name)
        try:
            response = self._bulk([{"delete": {"_index": index, "_id": i}} for i in ids])
        except Exception as exc:
            if error_status(exc) == 404:  # the collection is already gone
                return
            raise self._fail("delete", name, exc) from exc
        self._raise_item_errors(name, response, ignore_missing=True)

    def _raise_item_errors(self, name: str, response: Mapping[str, Any], *, ignore_missing: bool = False) -> None:
        if not response.get("errors"):
            return
        failures = []
        for item in response.get("items", []):
            result = next(iter(item.values()))
            if "error" not in result:
                continue
            if ignore_missing and result.get("status") == 404:
                continue
            reason = result["error"]
            failures.append(reason.get("reason", reason) if isinstance(reason, dict) else reason)
        if failures:
            raise StoreError(f"{self.label}: {len(failures)} record(s) were rejected on {name}: {failures[0]}")

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        entry = self._entry(name)
        if len(vector) != entry.dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {entry.dim}")
        if k < 1:
            return []
        if not all(math.isfinite(x) for x in vector):
            raise StoreError("query vector has a non-finite component")
        try:
            flt = compile_filter(filter, allowed=set(entry.fields))
        except ValueError as exc:
            raise StoreError(f"{self.label}: cannot compile the filter: {exc}") from exc
        k = min(k, MAX_WINDOW)
        try:
            response = self._knn(self._physical(name), entry, vector, k, flt)
        except Exception as exc:
            raise self._fail("query", name, exc) from exc
        hits = response["hits"]["hits"]
        matches = [Match(id=h["_id"], score=invert_score(entry.metric, float(h["_score"]))) for h in hits]
        matches.sort(key=lambda m: -m.score)
        return matches[:k]

    def num_candidates(self, k: int) -> int:
        return max(k, min(MAX_WINDOW, k * self._factor))

    def close(self) -> None:
        self._close()
