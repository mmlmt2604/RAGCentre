"""Turn a Docling ``DoclingDocument`` into RAGCentre's Document IR (schema 1.0).

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
``picture``, ``chart``             ``figure``, with the caption as ``text`` (may be empty); a picture
                                   description (``picture_description`` option) follows the caption
                                   after a blank line; the picture's class is
                                   ``metadata.classification``
``caption``, ``footnote``,         same name; ``code`` also gets ``metadata.language`` (when
``code``, ``formula``,             Docling knows it)
``page_header``, ``page_footer``
=================================  =========================================================

``page`` and ``bbox`` come from the item's first provenance entry; boxes are converted to a
top-left origin in page units, as the IR requires. An item that runs over a page break (a table
split across pages has one provenance entry per page) also gets ``metadata.page_end``, the last
page it touches, so a chunk made of it reports both pages.
"""

from __future__ import annotations

import warnings
from typing import Any

IR_VERSION = "1.0"

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
    last = max(int(p.page_no) for p in prov)
    if last > out["page"]:
        out["metadata"] = {"page_end": last}
    size = sizes.get(int(first.page_no))
    if size is not None and first.bbox is not None:
        box = first.bbox.to_top_left_origin(page_height=size[1])
        out["bbox"] = [float(box.l), float(box.t), float(box.r), float(box.b)]
    return out


def _annotations(item: Any) -> list[Any]:
    """The legacy ``annotations`` list (docling-core 2.x before ``meta``), without its deprecation warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return list(getattr(item, "annotations", None) or [])


def _picture_description(item: Any) -> str | None:
    """The text Docling's picture-description model wrote: ``meta.description`` in current
    docling-core, or a ``description`` annotation in older output."""
    text = getattr(getattr(getattr(item, "meta", None), "description", None), "text", None)
    if not text:
        for note in _annotations(item):
            if getattr(note, "kind", None) == "description":
                text = getattr(note, "text", None)
                break
    return str(text).strip() or None if text else None


def _picture_class(item: Any) -> str | None:
    """The picture classifier's best class (``meta.classification`` or a ``classification``
    annotation), e.g. ``bar_chart`` or ``logo``."""
    predictions = list(getattr(getattr(getattr(item, "meta", None), "classification", None), "predictions", None) or [])
    for note in _annotations(item):
        if getattr(note, "kind", None) == "classification":
            predictions.extend(getattr(note, "predicted_classes", None) or [])
    best = max(predictions, key=lambda p: getattr(p, "confidence", 0) or 0, default=None)
    name = getattr(best, "class_name", None)
    return str(name) if name else None


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
        caption = item.caption_text(doc) or ""
        description = _picture_description(item)
        block = {"type": "figure", "text": "\n\n".join(t for t in (caption, description) if t)}
        if classification := _picture_class(item):
            block["metadata"] = {"classification": classification}
    elif label in _SAME:
        block = {"type": label, "text": getattr(item, "text", "") or ""}
        language = getattr(getattr(item, "code_language", None), "value", None)
        if label == "code" and language and str(language).lower() != "unknown":
            block["metadata"] = {"language": str(language)}
    elif label in _PARAGRAPHS:
        block = {"type": "paragraph", "text": getattr(item, "text", "") or ""}
    elif hasattr(item, "text"):
        block = {"type": label, "text": item.text or ""}  # kept as metadata.original_type by RAGCentre
    else:
        return None  # groups and other structural nodes carry no text of their own
    if block["type"] != "figure" and not block["text"].strip():
        return None
    location = _location(item, sizes)
    extra = location.pop("metadata", None)
    block.update(location)
    if extra:
        block["metadata"] = {**block.get("metadata", {}), **extra}
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
    for item, _level in doc.iterate_items(traverse_pictures=True):
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
