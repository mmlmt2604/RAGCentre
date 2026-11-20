"""Pinecone vector store (optional extra ``ragcentre[pinecone]``, the ``pinecone`` package, v10.x).

**Mapping.** One RAGCentre collection is one Pinecone *namespace* inside one index that the user
created beforehand (``index_name``). RAGCentre never creates, configures or deletes a Pinecone
index: the index fixes the dimension and the metric, ``ensure_collection`` only checks that the
collection's spec matches them (``CollectionMismatch`` naming the index otherwise). The namespace
is ``prefix + collection name`` (so one index can serve several RAGCentre installations when they
use different prefixes). Namespaces appear when the first vector is written.

**Catalog.** Pinecone has nowhere to keep collection metadata, so dimension, metric and filter-field
types live in ``LocalCatalog`` (``<data dir>/store_catalogs/pinecone.db``), keyed by
``<index host or control-plane URL>/<index name>`` and the collection name. Losing that file
orphans the collections, exactly like losing the core database. ``drop_collection`` empties the
namespace (``delete(delete_all=True)``; a namespace that does not exist is fine) and forgets the
catalog entry.

**Facts verified** (pinecone 10.3.0 source, and the Pinecone Local emulator image
``ghcr.io/pinecone-io/pinecone-local``; see ``integration/t-pinecone.md`` for what was run and what
was only read):

* ``euclidean`` queries return the **squared** distance, so ``l2`` is ``-sqrt(d)``; ``cosine`` and
  ``dotproduct`` are the similarity and the dot product as is (run on the emulator, numerically).
* ``$ne`` and ``$nin`` also match records that lack the field, as ``models.filters.matches`` does;
  ``$in`` / ``$nin`` reject lists of booleans (run), so boolean lists are expanded to ``$or`` of
  ``$eq`` / ``$and`` of ``$ne``.
* Metadata values are strings, numbers or booleans (no nulls), 40 KB per record; numbers are
  stored as doubles, so integers beyond 2**53 are refused rather than rounded. IDs are 1 to 512
  ASCII characters. A query may ask for up to 10,000 results (1,000 when metadata is returned;
  this store never asks for metadata or values). An upsert request holds 1,000 vectors and 2 MB:
  ``max_batch`` is 1,000 and the store splits any batch further by encoded size.
* A cosine index cannot score an all-zero vector (the emulator returns a null score, which breaks
  the whole result set), so such vectors are refused at upsert and query time.

**Consistency.** Pinecone is eventually consistent: a write may not be visible to queries or to
``describe_index_stats`` (the source of ``collection_info().count``) for a moment. Nothing waits
for it here; the contract tests wait in their ``settle`` hook. ``filter_exact`` is declared
``False``: Pinecone documents no guarantee that a filtered top-k returns k matches when k exist,
and the only live evidence (the emulator, which answered every contract query exactly) says
nothing about the hosted service.

**Local use.** ``controller_host`` points the client's control plane at a Pinecone Local emulator
(``http://localhost:5080``). Its index description has the older ``dimension`` / ``metric`` shape
that the 10.x client cannot parse, so that one case is read with a plain ``GET /indexes/<name>``.

``pinecone`` is imported only when a store is created, so ``import ragcentre`` works without the
extra (and this module's name does not shadow the client: absolute imports resolve to it).
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar

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
from ragcentre.stores.catalog import Catalog, LocalCatalog
from ragcentre.stores.registry import VectorStoreType

PINECONE_METRIC: dict[Metric, str] = {"cosine": "cosine", "l2": "euclidean", "dot": "dotproduct"}
_FROM_PINECONE: dict[str, Metric] = {v: k for k, v in PINECONE_METRIC.items()}

MAX_UPSERT_COUNT = 1000  # vectors per upsert request
MAX_UPSERT_BYTES = 1_500_000  # our own budget under the 2 MB request limit (estimated from the JSON encoding)
MAX_DELETE_IDS = 1000
MAX_METADATA_BYTES = 40 * 1024
MAX_ID_LEN = 512
MAX_TOP_K = 10_000  # without metadata; 1,000 when metadata is returned (never requested here)
MAX_DIM = 20_000
MAX_SAFE_INT = 2**53  # numbers are doubles on the server
DEFAULT_NAMESPACE = "__default__"
LOCAL_API_KEY = "pinecone-local"  # the emulator accepts any key
LOCAL_API_VERSION = "2025-10"  # the emulator ignores it; its index description is the legacy shape
_SCHEMES = ("http://", "https://")
_AUTH_HINT = " (check the api_key secret and that it belongs to the project that owns the index)"


def _import() -> Any:
    return require_extra("pinecone", store="pinecone", extra="pinecone", package="pinecone")


# ---- validation -----------------------------------------------------------------------------------


def check_record_id(rid: str) -> str:
    if not isinstance(rid, str) or not rid or len(rid) > MAX_ID_LEN or not rid.isascii() or "\x00" in rid:
        raise StoreError(f"pinecone: record id {rid[:40]!r} must be 1 to {MAX_ID_LEN} ASCII characters")
    return rid


def check_metadata(rid: str, metadata: Mapping[str, Any]) -> None:
    """Refuse metadata Pinecone would reject or silently change (nulls, nesting, huge integers, 40 KB)."""
    for key, value in metadata.items():
        if not isinstance(key, str) or not key or key.startswith("$"):
            raise StoreError(
                f"pinecone: record {rid}: metadata key {key!r} must be a non-empty string not starting with '$'"
            )
        if isinstance(value, (bool, str)):
            continue
        if isinstance(value, int):
            if abs(value) > MAX_SAFE_INT:
                raise StoreError(
                    f"pinecone: record {rid}: metadata {key!r}={value} is beyond 2**53; Pinecone stores numbers as "
                    "doubles and would round it"
                )
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise StoreError(f"pinecone: record {rid}: metadata {key!r} must be a finite number")
        else:
            raise StoreError(
                f"pinecone: record {rid}: metadata {key!r} has unsupported type {type(value).__name__} "
                "(Pinecone metadata holds strings, numbers and booleans; there are no nulls)"
            )
    size = len(json.dumps(dict(metadata), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > MAX_METADATA_BYTES:
        raise StoreError(
            f"pinecone: record {rid}: metadata is {size} bytes; Pinecone allows {MAX_METADATA_BYTES} per record"
        )


def check_vector(rid: str, vector: Sequence[float], dim: int, metric: Metric) -> None:
    if len(vector) != dim:
        raise StoreError(f"pinecone: record {rid} has dimension {len(vector)}; the collection has {dim}")
    if not all(math.isfinite(x) for x in vector):
        raise StoreError(f"pinecone: record {rid}: vector values must be finite numbers")
    if metric == "cosine" and not any(vector):
        raise StoreError(
            f"pinecone: record {rid} is an all-zero vector, which a cosine index cannot score; "
            "use a different embedding input or a dot-product index"
        )


# ---- filter compiler ------------------------------------------------------------------------------


def _scalar(value: str | int | float | bool) -> str | int | float | bool:
    if isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise StoreError("pinecone: filter integers beyond 2**53 cannot be compared exactly")
        return value
    if not math.isfinite(value):
        raise StoreError("pinecone: filter values must be finite numbers")
    return value


def compile_filter(f: Filter) -> dict[str, Any]:
    """Compile a parsed filter to a Pinecone metadata filter.

    The language is nearly the identity: ``$eq``, ``$ne``, ``$gt``, ``$gte``, ``$lt``, ``$lte``, ``$in``,
    ``$nin``, ``$and`` and ``$or``. The one rewrite is lists of booleans, which Pinecone refuses in
    ``$in`` / ``$nin``: ``$in`` becomes an ``$or`` of ``$eq`` and ``$nin`` an ``$and`` of ``$ne``.
    ``$ne`` and ``$nin`` match records that lack the field, which is what ``matches`` says too.
    """
    if isinstance(f, (And, Or)):
        return {("$and" if isinstance(f, And) else "$or"): [compile_filter(i) for i in f.items]}
    assert isinstance(f, Cond)
    if isinstance(f.value, tuple):
        values = [_scalar(v) for v in f.value]
        if any(isinstance(v, bool) for v in values):
            if f.op == "$in":
                return {"$or": [{f.field: {"$eq": v}} for v in values]}
            return {"$and": [{f.field: {"$ne": v}} for v in values]}
        return {f.field: {f.op: values}}
    return {f.field: {f.op: _scalar(f.value)}}


def to_score(metric: Metric, raw: float) -> float:
    """Pinecone's score to the plan's score: cosine and dot as is, euclidean is the squared distance."""
    if metric == "l2":
        return -math.sqrt(max(raw, 0.0))
    return raw


# ---- index description ----------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexShape:
    dim: int
    metric: Metric
    host: str  # data-plane host, with a scheme


def _with_scheme(host: str, like: str | None) -> str:
    if host.startswith(_SCHEMES):
        return host.rstrip("/")
    scheme = "http://" if like is not None and like.startswith("http://") else "https://"
    return scheme + host.rstrip("/")


def shape_from_model(name: str, model: Any, index_host: str | None) -> IndexShape:
    """Dimension, metric and host from the client's ``IndexModel`` (2026-07 schema form)."""
    status = getattr(model, "status", None)
    if status is not None and not getattr(status, "ready", True):
        raise StoreError(f"pinecone: index {name!r} is not ready yet (state {getattr(status, 'state', 'unknown')!r})")
    fields: Mapping[str, Any] = getattr(getattr(model, "schema", None), "fields", None) or {}
    dense = fields.get("_values")
    extra = sorted(k for k in fields if k not in ("_values", "_sparse_values"))
    dim, pmetric = getattr(dense, "dimension", None), getattr(dense, "metric", None)
    if dense is None or extra or not isinstance(dim, int) or pmetric not in _FROM_PINECONE:
        raise StoreError(
            f"pinecone: index {name!r} is not a plain dense vector index (schema fields: {sorted(fields) or 'none'}); "
            "create an index with a dimension and a cosine, euclidean or dotproduct metric"
        )
    host = index_host or getattr(model, "host", None)
    if not host:
        raise StoreError(f"pinecone: index {name!r} has no host yet")
    return IndexShape(dim, _FROM_PINECONE[pmetric], _with_scheme(host, None))


def shape_from_legacy_json(name: str, body: Any, controller: str, index_host: str | None) -> IndexShape:
    """The same from the older ``{"dimension", "metric", "host"}`` description (Pinecone Local)."""
    if not isinstance(body, dict):
        raise StoreError(f"pinecone: unexpected description of index {name!r}")
    status = body.get("status")
    if isinstance(status, dict) and status.get("ready") is False:
        raise StoreError(f"pinecone: index {name!r} is not ready yet (state {status.get('state')!r})")
    dim, pmetric, host = body.get("dimension"), body.get("metric"), index_host or body.get("host")
    if body.get("vector_type") not in (None, "dense") or type(dim) is not int or pmetric not in _FROM_PINECONE:
        raise StoreError(f"pinecone: index {name!r} is not a plain dense vector index")
    if not isinstance(host, str) or not host:
        raise StoreError(f"pinecone: index {name!r} has no host yet")
    return IndexShape(dim, _FROM_PINECONE[pmetric], _with_scheme(host, controller))


# ---- the store ------------------------------------------------------------------------------------


class PineconeStore:
    # The type's best case. An instance narrows ``metrics`` to the one metric its index has (see ``capabilities``).
    BEST_CASE: ClassVar[Capabilities] = Capabilities(
        metrics=frozenset({"cosine", "l2", "dot"}),
        max_batch=MAX_UPSERT_COUNT,
        max_dim=MAX_DIM,
        consistency="eventual",
        filter_exact=False,  # see the module docstring
        max_collections=None,  # the namespace quota depends on the plan (100 to 1,000,000)
    )

    def __init__(
        self,
        pc: Any,
        px: Any,
        catalog: Catalog,
        *,
        index_name: str,
        prefix: str = "",
        controller_host: str | None = None,
        index_host: str | None = None,
        api_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        """``pc`` is a ``pinecone.Pinecone`` (or a stand-in), ``px`` the ``pinecone`` module (its error classes)."""
        self._pc = pc
        self._px = px
        self._catalog = catalog
        self._index_name = index_name
        self._prefix = prefix
        self._controller = controller_host
        self._index_host = index_host
        self._api_key = api_key
        self._timeout = timeout
        self._lock = threading.Lock()
        self._shape: IndexShape | None = None
        self._index: Any = None
        self._caps: Capabilities | None = None

    @property
    def capabilities(self) -> Capabilities:
        """Per instance: a Pinecone index has one metric, so that is the only one this store offers.

        The first read describes the index. If that fails (no network, no such index) the type's best case is
        returned and the next read tries again; every operation reports the real error itself.
        """
        if self._caps is None:
            with contextlib.suppress(StoreError):
                self._resolve()
        return self._caps or self.BEST_CASE

    @classmethod
    def connect(
        cls,
        *,
        index_name: str,
        data_dir: Path,
        api_key: str | None = None,
        prefix: str = "",
        controller_host: str | None = None,
        index_host: str | None = None,
        timeout: float = 30.0,
    ) -> PineconeStore:
        px = _import()
        key = api_key or (LOCAL_API_KEY if controller_host else "")
        if not key:
            raise StoreError("pinecone: the api_key secret is required")
        try:
            pc = px.Pinecone(api_key=key, host=controller_host, timeout=timeout, source_tag="ragcentre")
        except Exception as exc:  # the SDK validates keys and hosts
            raise StoreError(
                f"pinecone: could not create the client: {type(exc).__name__}: {exc}".replace(key, "***")
            ) from None
        target = index_host or controller_host or "pinecone"
        catalog = LocalCatalog(data_dir, "pinecone", instance_key=f"{target.rstrip('/')}/{index_name}")
        return cls(
            pc,
            px,
            catalog,
            index_name=index_name,
            prefix=prefix,
            controller_host=controller_host,
            index_host=index_host,
            api_key=key,
            timeout=timeout,
        )

    # ---- helpers ------------------------------------------------------------------------------

    def _namespace(self, name: str) -> str:
        check_collection_name(name)
        ns = self._prefix + name
        if not ns.isascii() or len(ns) > 512 or ns == DEFAULT_NAMESPACE:
            raise StoreError(f"pinecone: collection name {ns!r} is not a usable namespace")
        return ns

    def _wrap(self, what: str, exc: Exception) -> StoreError:
        px = self._px
        hint = _AUTH_HINT if isinstance(exc, (px.UnauthorizedError, px.ForbiddenError)) else ""
        text = str(exc).replace(self._api_key, "***") if self._api_key else str(exc)
        return StoreError(f"pinecone: {what} failed: {text}{hint}")

    def _describe(self) -> IndexShape:
        px, name = self._px, self._index_name
        missing = (
            f"pinecone: index {name!r} does not exist in this project. RAGCentre never creates Pinecone indexes: "
            "create it first (a dense index with the embedding model's dimension and metric)"
        )
        try:
            model = self._pc.indexes.describe(name)
        except px.NotFoundError:
            raise StoreError(missing) from None
        except px.ResponseParsingError as exc:
            if self._controller is None:
                raise self._wrap("describing the index", exc) from None
            return self._describe_legacy(self._controller, missing)
        except px.PineconeError as exc:
            raise self._wrap("describing the index", exc) from None
        return shape_from_model(name, model, self._index_host)

    def _describe_legacy(self, controller: str, missing: str) -> IndexShape:
        import httpx

        url = controller.rstrip("/") + f"/indexes/{self._index_name}"
        try:
            reply = httpx.get(
                url,
                headers={"Api-Key": self._api_key, "X-Pinecone-Api-Version": LOCAL_API_VERSION},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise StoreError(f"pinecone: describing the index failed: {type(exc).__name__}") from None
        if reply.status_code == 404:
            raise StoreError(missing)
        if reply.status_code >= 400:
            raise StoreError(f"pinecone: describing the index failed: HTTP {reply.status_code}")
        try:
            body = reply.json()
        except ValueError:
            raise StoreError("pinecone: the index description is not JSON") from None
        return shape_from_legacy_json(self._index_name, body, controller, self._index_host)

    def _resolve(self, *, refresh: bool = False) -> tuple[Any, IndexShape]:
        """The data-plane client and the index shape; the shape of an index never changes, so it is cached."""
        with self._lock:
            if refresh or self._shape is None or self._index is None:
                shape = self._describe()
                if self._index is None or (self._shape is not None and self._shape.host != shape.host):
                    if self._index is not None:
                        self._index.close()
                    try:
                        self._index = self._pc.index(host=shape.host)
                    except self._px.PineconeError as exc:
                        raise self._wrap("opening the index", exc) from None
                self._shape = shape
                self._caps = replace(self.BEST_CASE, metrics=frozenset({shape.metric}))
            return self._index, self._shape

    def _entry(self, name: str) -> Any:
        entry = self._catalog.get(name)
        if entry is None:
            raise CollectionNotFound(f"pinecone: collection {name} does not exist")
        return entry

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        index, _ = self._resolve(refresh=True)
        try:
            index.describe_index_stats()
        except self._px.PineconeError as exc:
            raise self._wrap("reaching the index", exc) from None

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.BEST_CASE)  # the metric is checked against the index below, with a clearer error
        self._namespace(spec.name)
        _, shape = self._resolve(refresh=True)
        if spec.dim != shape.dim or spec.metric != shape.metric:
            raise CollectionMismatch(
                f"Pinecone index {self._index_name!r} has dimension {shape.dim} and metric {shape.metric}; "
                f"collection {spec.name} needs dimension {spec.dim} and metric {spec.metric}. "
                "A Pinecone index has one dimension and metric: use another index, or another embedding model"
            )
        self._catalog.ensure(spec, {})

    def collection_info(self, name: str) -> CollectionInfo | None:
        ns = self._namespace(name)
        entry = self._catalog.get(name)
        if entry is None:
            return None
        index, shape = self._resolve()
        try:
            stats = index.describe_index_stats()
        except self._px.PineconeError as exc:
            raise self._wrap("describing the index stats", exc) from None
        summary = (stats.namespaces or {}).get(ns)
        count = int(getattr(summary, "vector_count", 0) or 0)
        # The index is the truth for dimension and metric (the catalog only adds the field types).
        return CollectionInfo(shape.dim, shape.metric, count)

    def drop_collection(self, name: str) -> None:
        ns = self._namespace(name)
        index, _ = self._resolve()
        try:
            index.delete(delete_all=True, namespace=ns)
        except self._px.NotFoundError:
            pass  # the namespace does not exist (never written, or already gone)
        except self._px.PineconeError as exc:
            raise self._wrap(f"emptying namespace {ns!r}", exc) from None
        self._catalog.drop(name)

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > MAX_UPSERT_COUNT:
            raise StoreError(f"pinecone: batch of {len(records)} exceeds max_batch {MAX_UPSERT_COUNT}")
        ns = self._namespace(name)
        entry = self._entry(name)
        items: list[tuple[dict[str, Any], int]] = []
        for rec in records:
            check_record_id(rec.id)
            check_vector(rec.id, rec.vector, entry.dim, entry.metric)
            check_metadata(rec.id, rec.metadata)
            item = {"id": rec.id, "values": list(rec.vector), "metadata": dict(rec.metadata)}
            items.append((item, len(json.dumps(item, separators=(",", ":")))))
        if not items:
            return
        index, _ = self._resolve()
        chunk: list[dict[str, Any]] = []
        size = 0
        for item, n in items:
            if chunk and (len(chunk) >= MAX_UPSERT_COUNT or size + n > MAX_UPSERT_BYTES):
                self._send(index, ns, chunk)
                chunk, size = [], 0
            chunk.append(item)
            size += n
        self._send(index, ns, chunk)

    def _send(self, index: Any, ns: str, chunk: list[dict[str, Any]]) -> None:
        try:
            result = index.upsert(vectors=chunk, namespace=ns)
        except self._px.PineconeError as exc:
            raise self._wrap(f"upserting {len(chunk)} vectors", exc) from None
        done = getattr(result, "upserted_count", len(chunk))
        if done != len(chunk):
            raise StoreError(f"pinecone: upserted {done} of {len(chunk)} vectors")

    def delete(self, name: str, ids: Sequence[str]) -> None:
        ns = self._namespace(name)
        if not ids or self._catalog.get(name) is None:
            return
        for rid in ids:
            check_record_id(rid)
        index, _ = self._resolve()
        for start in range(0, len(ids), MAX_DELETE_IDS):
            try:
                index.delete(ids=list(ids[start : start + MAX_DELETE_IDS]), namespace=ns)
            except self._px.NotFoundError:
                return  # no such namespace: nothing to delete
            except self._px.PineconeError as exc:
                raise self._wrap("deleting vectors", exc) from None

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        ns = self._namespace(name)
        entry = self._entry(name)
        if k <= 0:
            return []
        if k > MAX_TOP_K:
            raise StoreError(f"pinecone: k={k} is above the {MAX_TOP_K} results one Pinecone query can return")
        check_vector("(query)", vector, entry.dim, entry.metric)
        body = compile_filter(filter) if filter is not None else None
        index, _ = self._resolve()
        try:
            reply = index.query(
                top_k=k,
                vector=list(vector),
                namespace=ns,
                filter=body,
                include_values=False,
                include_metadata=False,
            )
        except self._px.PineconeError as exc:
            raise self._wrap("querying", exc) from None
        matches = [Match(m.id, to_score(entry.metric, float(m.score))) for m in reply.matches]
        matches.sort(key=lambda m: -m.score)  # Pinecone orders best first; squared distances ascend
        return matches[:k]

    def close(self) -> None:
        with self._lock:
            index, self._index = self._index, None
            if index is not None:
                index.close()
            self._pc.close()
        close = getattr(self._catalog, "close", None)
        if close is not None:
            close()


# ---- registration ---------------------------------------------------------------------------------


class PineconeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index_name: str = Field(
        description="Name of the Pinecone index you created (dense; its dimension and metric must match the embedder). "
        "RAGCentre never creates or deletes indexes",
    )
    prefix: str = Field(
        default="", description="Prefix of every namespace this store uses (one namespace per RAGCentre collection)"
    )
    controller_host: str | None = Field(
        default=None,
        description="Control-plane URL for Pinecone Local, e.g. http://localhost:5080; leave empty for Pinecone's cloud",
    )
    index_host: str | None = Field(
        default=None,
        description="The index's data-plane host, if you want to override the one Pinecone reports (private endpoints)",
    )
    timeout: float = Field(default=30.0, gt=0, le=600, description="Request timeout in seconds")

    @field_validator("index_name")
    @classmethod
    def _check_index_name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,43}[a-z0-9])?", v):
            raise ValueError("index_name must be 1 to 45 characters: lower-case letters, digits and '-'")
        return v

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        if len(v) > 64 or not re.fullmatch(r"[A-Za-z0-9_.:-]*", v):
            raise ValueError("prefix may hold at most 64 letters, digits, '_', '.', ':' and '-'")
        return v

    @field_validator("controller_host", "index_host")
    @classmethod
    def _check_host(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        rest = v.removeprefix("https://").removeprefix("http://")
        if not rest or re.search(r"[\s@?#]", v) or "://" in rest:
            raise ValueError("expected a host or URL such as http://localhost:5080 (no credentials, query or fragment)")
        return v


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> PineconeStore:
    assert isinstance(config, PineconeConfig)
    return PineconeStore.connect(
        index_name=config.index_name,
        data_dir=data_dir,
        api_key=secrets.get("api_key") or None,
        prefix=config.prefix,
        controller_host=config.controller_host,
        index_host=config.index_host,
        timeout=config.timeout,
    )


STORE_TYPE = VectorStoreType(
    type="pinecone",
    label="Pinecone",
    description=(
        "Pinecone index you created, one namespace per collection. Eventually consistent. "
        "Needs the optional 'pinecone' extra."
    ),
    config_model=PineconeConfig,
    secret_keys=("api_key",),
    capabilities=PineconeStore.BEST_CASE,
    factory=_factory,
)
