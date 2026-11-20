"""Vector store type registry, loaded from the ``ragcentre.vector_stores`` entry-point group."""

from __future__ import annotations

import builtins
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ragcentre.stores.base import Capabilities, VectorStore

log = logging.getLogger("ragcentre.stores")

GROUP = "ragcentre.vector_stores"
BUILTIN = {
    "sqlite_vec": "ragcentre.stores.sqlite_vec:STORE_TYPE",
    # Optional-extra adapters import their client library lazily, so listing them is always safe.
    "qdrant": "ragcentre.stores.qdrant:STORE_TYPE",
    "pgvector": "ragcentre.stores.pgvector:STORE_TYPE",
    "chroma": "ragcentre.stores.chroma:STORE_TYPE",
    "lancedb": "ragcentre.stores.lancedb:STORE_TYPE",
    "redis": "ragcentre.stores.redis:STORE_TYPE",
    "weaviate": "ragcentre.stores.weaviate:STORE_TYPE",
    "elasticsearch": "ragcentre.stores.elasticsearch:STORE_TYPE",
    "opensearch": "ragcentre.stores.opensearch:STORE_TYPE",
    "milvus": "ragcentre.stores.milvus:STORE_TYPE",
    "pinecone": "ragcentre.stores.pinecone:STORE_TYPE",
    "http": "ragcentre.stores.http:STORE_TYPE",
}

StoreFactory = Callable[[BaseModel, dict[str, str], Path], "VectorStore"]


@dataclass(frozen=True)
class VectorStoreType:
    type: str
    label: str
    description: str
    config_model: builtins.type[BaseModel]
    secret_keys: tuple[str, ...]
    capabilities: Capabilities  # the type's best case, for listing; an instance may report less
    factory: StoreFactory
    # Config fields that shape the index (quantization, HNSW parameters, vector type). They cannot
    # change after the provider is created: changing one means a new store provider, so a new index.
    # Connection fields (url, timeout, credentials) stay mutable.
    immutable_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = [f for f in self.immutable_fields if f not in self.config_model.model_fields]
        if unknown:
            raise ValueError(f"store type {self.type!r} lists immutable_fields not in its config model: {unknown}")


def _load(target: str) -> VectorStoreType:
    module_name, _, attr = target.partition(":")
    module = __import__(module_name, fromlist=[attr])
    value = getattr(module, attr)
    if not isinstance(value, VectorStoreType):
        raise TypeError(f"{target} is not a VectorStoreType")
    return value


@cache
def vector_store_types() -> Mapping[str, VectorStoreType]:
    found: dict[str, VectorStoreType] = {}
    for name, target in BUILTIN.items():
        found[name] = _load(target)
    for ep in entry_points(group=GROUP):
        if ep.name in found:
            continue
        try:
            value = ep.load()
        except Exception:  # an optional or broken plugin must not break start-up
            log.warning("could not load vector store type", extra={"entry_point": ep.name}, exc_info=True)
            continue
        if isinstance(value, VectorStoreType):
            found[ep.name] = value
    return found
