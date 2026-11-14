"""Chunk strategies, registered through the ``ragcentre.chunk_strategies`` entry-point group."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import cache
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any

from ragcentre.chunking.base import (
    Chunk,
    ChunkDraft,
    ChunkStrategy,
    embedded_text,
    finalize,
    options_with_defaults,
    validate_options,
)
from ragcentre.models.ir import DocumentIR

log = logging.getLogger("ragcentre.chunking")

GROUP = "ragcentre.chunk_strategies"
BUILTIN = {
    "fixed": "ragcentre.chunking.fixed:FixedStrategy",
    "recursive": "ragcentre.chunking.recursive:RecursiveStrategy",
    "structure": "ragcentre.chunking.structure:StructureStrategy",
    "page": "ragcentre.chunking.page:PageStrategy",
    "http": "ragcentre.chunking.http:HttpStrategy",
}


def _load(target: str) -> ChunkStrategy:
    module_name, _, attr = target.partition(":")
    cls = getattr(import_module(module_name), attr)
    strategy: ChunkStrategy = cls()
    return strategy


@cache
def strategies() -> Mapping[str, ChunkStrategy]:
    found: dict[str, ChunkStrategy] = {name: _load(target) for name, target in BUILTIN.items()}
    for ep in entry_points(group=GROUP):
        if ep.name in found:
            continue
        try:
            value = ep.load()
            found[ep.name] = value() if isinstance(value, type) else value
        except Exception:
            log.warning("could not load chunk strategy", extra={"entry_point": ep.name}, exc_info=True)
    return found


def get_strategy(name: str) -> ChunkStrategy | None:
    return strategies().get(name)


def run_strategy(name: str, doc: DocumentIR, options: Any, provider: Any = None) -> list[Chunk]:
    strategy = strategies()[name]
    model = validate_options(strategy, options) if isinstance(options, dict) or options is None else options
    return finalize(doc, strategy.chunk(doc, model, provider))


__all__ = [
    "Chunk",
    "ChunkDraft",
    "ChunkStrategy",
    "embedded_text",
    "finalize",
    "get_strategy",
    "options_with_defaults",
    "run_strategy",
    "strategies",
    "validate_options",
]
