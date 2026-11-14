"""The vector store interface (section 10.1) and the core's batching helper."""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import ClassVar, Literal, Protocol, runtime_checkable

from ragcentre.models.filters import FieldType, Filter

Metric = Literal["cosine", "l2", "dot"]
METRICS: tuple[Metric, ...] = ("cosine", "l2", "dot")
COLLECTION_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")


@dataclass(frozen=True)
class Capabilities:
    metrics: frozenset[Metric]
    max_batch: int  # largest upsert batch the adapter accepts
    multi_vector: bool = False  # reserved; version 1 never uses it
    native_hybrid: bool = False  # reserved


@dataclass(frozen=True)
class CollectionSpec:
    name: str
    dim: int
    metric: Metric
    fields: dict[str, FieldType] = field(default_factory=dict)  # filterable metadata fields
    vector_kind: Literal["dense", "multi"] = "dense"  # "multi" is rejected in version 1


@dataclass(frozen=True)
class Record:
    id: str  # UUID string
    vector: list[float]
    metadata: dict[str, str | int | float | bool]


@dataclass(frozen=True)
class Match:
    id: str
    score: float  # higher is always better


@dataclass(frozen=True)
class CollectionInfo:
    dim: int
    metric: Metric
    count: int


class StoreError(Exception):
    """A vector store operation failed (connection, missing collection, bad input)."""


class StoreUnavailable(StoreError):
    """The adapter cannot run in this environment (missing extra, missing SQLite feature)."""


class CollectionNotFound(StoreError):
    pass


@runtime_checkable
class VectorStore(Protocol):
    capabilities: ClassVar[Capabilities]

    def healthcheck(self) -> None: ...
    def ensure_collection(self, spec: CollectionSpec) -> None: ...
    def collection_info(self, name: str) -> CollectionInfo | None: ...
    def drop_collection(self, name: str) -> None: ...
    def upsert(self, name: str, records: Sequence[Record]) -> None: ...
    def delete(self, name: str, ids: Sequence[str]) -> None: ...
    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]: ...
    def close(self) -> None: ...


def require_extra(module: str, *, store: str, extra: str, package: str) -> ModuleType:
    """Import an optional client library, or raise ``StoreUnavailable`` naming the missing extra.

    Adapters call this lazily (never at module import), so ``import ragcentre`` works without any
    extras. RAGCentre never installs packages itself; the error only says what is missing.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise StoreUnavailable(
            f"The {store} vector store needs the optional '{extra}' extra (ragcentre[{extra}], "
            f"which provides the {package} package), and it is not installed."
        ) from exc


def check_collection_name(name: str) -> str:
    if not COLLECTION_RE.match(name):
        raise StoreError(f"invalid collection name {name!r}")
    return name


def check_spec(spec: CollectionSpec, capabilities: Capabilities) -> None:
    check_collection_name(spec.name)
    if spec.vector_kind != "dense":
        raise StoreError("multi-vector collections are not supported in this version")
    if spec.metric not in capabilities.metrics:
        raise StoreError(f"metric {spec.metric!r} is not supported by this store")
    if spec.dim < 1:
        raise StoreError("dimension must be at least 1")


def batches(records: Sequence[Record], size: int) -> Iterator[Sequence[Record]]:
    if size < 1:
        raise ValueError("batch size must be at least 1")
    for start in range(0, len(records), size):
        yield records[start : start + size]


def upsert_batched(store: VectorStore, name: str, records: Sequence[Record]) -> None:
    """Upsert in batches no larger than the store's ``max_batch`` (the core never sends more)."""
    for chunk in batches(records, store.capabilities.max_batch):
        store.upsert(name, chunk)


def delete_batched(store: VectorStore, name: str, ids: Sequence[str]) -> None:
    size = store.capabilities.max_batch
    for start in range(0, len(ids), size):
        store.delete(name, ids[start : start + size])
