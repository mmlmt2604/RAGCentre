"""Chunk-to-target matching (section 14.3). Ground truth never uses chunk ids."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Unicode NFKC, casefold, and collapse every whitespace run to one space."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def matches_target(chunk: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    """``chunk`` has ``document_id``, ``text``, ``page_start``, ``page_end`` (its own text only)."""
    if chunk["document_id"] != target["document_id"]:
        return False
    pages = target.get("pages")
    if pages:
        start, end = chunk.get("page_start"), chunk.get("page_end")
        if start is None or end is None:
            return False
        if not any(start <= p <= end for p in pages):
            return False
    snippet = target.get("text_contains")
    return not (snippet and normalise(snippet) not in normalise(str(chunk["text"])))


def matched_targets(chunk: Mapping[str, Any], targets: Sequence[Mapping[str, Any]]) -> list[int]:
    return [i for i, t in enumerate(targets) if matches_target(chunk, t)]
