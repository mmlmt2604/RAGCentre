"""Turn a Docling ``DoclingDocument`` into RAGCentre's Document IR (schema 1.1).

This module needs only ``docling-core`` (the document model), not the full Docling converter, so
it can be tested without models.

Mapping:

=================================  =========================================================
Docling label                      IR block
=================================  =========================================================
``title``                          ``heading`` level 1
``section_header`` (level n)       ``heading`` level n + 1 (capped at 6)
``text``, ``paragraph``, other     ``paragraph`` (unknown labels keep their name; RAGCentre
text-like labels                   stores it as ``metadata.original_type``)
``list_item``                      ``list_item``
``table``                          ``table``: Markdown in ``text``, HTML in ``html``
``picture``, ``chart``             ``figure``, with the caption as ``text`` (may be empty)
``caption``, ``footnote``,         same name
``code``, ``formula``,
``page_header``, ``page_footer``
=================================  =========================================================

``page`` and ``bbox`` come from the item's first provenance entry; boxes are converted to a
top-left origin in page units, as the IR requires. An item with more than one provenance entry (a
paragraph or table that runs over a page break) also gets ``spans``: every entry, in order.

A picture's classification (the most confident class) and description are read from
``item.meta`` (``docling-core`` 2.x) or, failing that, ``item.annotations``; they go to
``metadata.classification`` and ``metadata.description``, and the description is also appended to
the figure's ``text`` after the caption and a blank line, so the built-in chunkers can find it.
"""

from __future__ import annotations

import warnings
from typing import Any

IR_VERSION = "1.1"

_SAME = {"list_item", "caption", "footnote", "code", "formula", "page_header", "page_footer"}
_FIGURES = {"picture", "chart"}
_PARAGRAPHS = {"text", "paragraph", "reference", "handwritten_text"}


def _label(item: Any) -> str:
    label = getattr(item, "label", None)
    return str(getattr(label, "value", label) or "text")


def _page_sizes(doc: Any) -> dict[int, tuple[float, float]]:
    return {int(no): (float(p.size.width), float(p.size.height)) for no, p in (doc.pages or {}).items()}


def _location(item: Any, sizes: dict[int, tuple[float, float]]) -> dict[str, Any]:
    prov = getattr(item, "prov", None) or []
    if not prov:
        return {}
    first = prov[0]
    out: dict[str, Any] = {"page": int(first.page_no)}
    size = sizes.get(int(first.page_no))
    if size is not None and first.bbox is not None:
        box = first.bbox.to_top_left_origin(page_height=size[1])
        out["bbox"] = [float(box.l), float(box.t), float(box.r), float(box.b)]
    return out


def _span(prov: Any, sizes: dict[int, tuple[float, float]]) -> dict[str, Any] | None:
    size = sizes.get(int(prov.page_no))
    if size is None or prov.bbox is None:
        return None
    box = prov.bbox.to_top_left_origin(page_height=size[1])
    return {"page": int(prov.page_no), "bbox": [float(box.l), float(box.t), float(box.r), float(box.b)]}


def _spans(item: Any, sizes: dict[int, tuple[float, float]]) -> list[dict[str, Any]]:
    prov = getattr(item, "prov", None) or []
    if len(prov) < 2:
        return []
    return [span for entry in prov if (span := _span(entry, sizes)) is not None]


def _figure_details(item: Any) -> tuple[str, str]:
    """``(description, classification)`` of a picture; ``""`` for what the engine did not produce."""
    description = classification = ""
    meta = getattr(item, "meta", None)
    if meta is not None:
        text = getattr(getattr(meta, "description", None), "text", None)
        description = str(text or "").strip()
        predictions = list(getattr(getattr(meta, "classification", None), "predictions", None) or [])
        if predictions:
            best = max(predictions, key=lambda p: float(getattr(p, "confidence", 0) or 0))
            classification = str(getattr(best, "class_name", "") or "")
    if not (description and classification):
        with warnings.catch_warnings():  # ``annotations`` is deprecated in docling-core in favour of ``meta``
            warnings.simplefilter("ignore", DeprecationWarning)
            annotations = list(getattr(item, "annotations", None) or [])
        for annotation in annotations:
            kind = getattr(annotation, "kind", "")
            if kind == "description" and not description:
                description = str(getattr(annotation, "text", "") or "").strip()
            elif kind == "classification" and not classification:
                classes = list(getattr(annotation, "predicted_classes", None) or [])
                if classes:
                    best = max(classes, key=lambda c: float(getattr(c, "confidence", 0) or 0))
                    classification = str(getattr(best, "class_name", "") or "")
    return description, classification


def _block(item: Any, doc: Any, sizes: dict[int, tuple[float, float]]) -> dict[str, Any] | None:
    label = _label(item)
    block: dict[str, Any]
    if label == "title":
        block = {"type": "heading", "level": 1, "text": item.text}
    elif label == "section_header":
        block = {"type": "heading", "level": min(6, int(getattr(item, "level", 1) or 1) + 1), "text": item.text}
    elif label == "table":
        block = {"type": "table", "text": item.export_to_markdown(doc=doc)}
        html = item.export_to_html(doc=doc, add_caption=False)
        if html:
            block["html"] = html
    elif label in _FIGURES:
        description, classification = _figure_details(item)
        caption = (item.caption_text(doc) or "").strip()
        block = {"type": "figure", "text": "\n\n".join(part for part in (caption, description) if part)}
        details = {
            key: value for key, value in (("description", description), ("classification", classification)) if value
        }
        if details:
            block["metadata"] = details
    elif label in _SAME:
        block = {"type": label, "text": getattr(item, "text", "") or ""}
    elif label in _PARAGRAPHS:
        block = {"type": "paragraph", "text": getattr(item, "text", "") or ""}
    elif hasattr(item, "text"):
        block = {"type": label, "text": item.text or ""}  # kept as metadata.original_type by RAGCentre
    else:
        return None  # groups and other structural nodes carry no text of their own
    if block["type"] != "figure" and not block["text"].strip():
        return None
    block.update(_location(item, sizes))
    if spans := _spans(item, sizes):
        block["spans"] = spans
    return block


def _page_image(page: Any) -> dict[str, str] | None:
    image = getattr(page, "image", None)
    uri = str(getattr(image, "uri", "") or "")
    if not uri.startswith("data:") or ";base64," not in uri:
        return None
    header, data = uri.split(",", 1)
    mime = header[len("data:") :].split(";", 1)[0] or "image/png"
    return {"mime": mime, "data_b64": data}


def to_ir(doc: Any, *, include_page_images: bool = False) -> dict[str, Any]:
    """Convert a ``DoclingDocument`` to a Document IR ``dict``."""
    sizes = _page_sizes(doc)
    blocks: list[dict[str, Any]] = []
    for item, _level in doc.iterate_items():
        block = _block(item, doc, sizes)
        if block is not None:
            block["id"] = f"b{len(blocks) + 1}"
            blocks.append(block)
    pages: list[dict[str, Any]] = []
    for no in sorted(sizes):
        width, height = sizes[no]
        page: dict[str, Any] = {"page": no, "width": width, "height": height}
        if include_page_images:
            image = _page_image(doc.pages[no])
            if image is not None:
                page["image"] = image
        pages.append(page)
    metadata: dict[str, Any] = {"page_count": len(pages)} if pages else {}
    title = next((b["text"] for b in blocks if b["type"] == "heading" and b["level"] == 1), None)
    if title:
        metadata["title"] = title
    if getattr(doc, "name", None):
        metadata["source_name"] = doc.name
    return {"schema_version": IR_VERSION, "metadata": metadata, "pages": pages, "blocks": blocks}
