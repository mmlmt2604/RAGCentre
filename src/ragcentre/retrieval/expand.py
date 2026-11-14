"""Neighbour expansion (section 13): adjacent chunks by ordinal in the same document."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def neighbours(chunks: Sequence[Any], ordinal: int, count: int) -> tuple[list[str], list[str]]:
    """Up to ``count`` chunk texts before and after ``ordinal``, in reading order.

    ``chunks`` are rows with ``ordinal`` and ``text`` from one document and chunk node.
    """
    if count <= 0:
        return [], []
    ordered = sorted(chunks, key=lambda c: int(c["ordinal"]))
    before = [str(c["text"]) for c in ordered if int(c["ordinal"]) < ordinal]
    after = [str(c["text"]) for c in ordered if int(c["ordinal"]) > ordinal]
    return before[-count:], after[:count]
