"""Assemble a Document IR (schema 1.0) from blocks and pages that a shim's ``convert`` built."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

IR_VERSION = "1.0"


def build_ir(
    blocks: Iterable[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    *,
    source_name: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Number the blocks and add the document metadata.

    Blocks whose text is blank are dropped unless they are figures. Ids are ``b1`` to ``bn`` in
    the order given (any ``id`` already present is replaced). ``metadata.page_count`` is set when
    there are pages, ``metadata.title`` to the text of the first level-1 heading, and
    ``metadata.source_name`` when given; those override the same keys in ``metadata``. The inputs
    are not modified.
    """
    kept: list[dict[str, Any]] = []
    for block in blocks:
        if block["type"] != "figure" and not str(block.get("text", "")).strip():
            continue
        kept.append({**block, "id": f"b{len(kept) + 1}"})
    meta: dict[str, Any] = dict(metadata or {})
    if pages:
        meta["page_count"] = len(pages)
    title = next((b["text"] for b in kept if b["type"] == "heading" and b.get("level") == 1), None)
    if title:
        meta["title"] = title
    if source_name:
        meta["source_name"] = source_name
    return {"schema_version": IR_VERSION, "metadata": meta, "pages": [dict(p) for p in pages], "blocks": kept}
