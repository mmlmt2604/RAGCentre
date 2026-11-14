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
}

StoreFactory = Callable[[BaseModel, dict[str, str], Path], "VectorStore"]


@dataclass(frozen=True)
class VectorStoreType:
    type: str
    label: str
    description: str
    config_model: builtins.type[BaseModel]
    secret_keys: tuple[str, ...]
    capabilities: Capabilities
    factory: StoreFactory


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
