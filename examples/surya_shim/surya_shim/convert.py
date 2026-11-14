"""Turn Surya page results (reduced to plain dicts by ``engine.py``) into RAGCentre's Document IR
(schema 1.0). This module imports nothing from Surya, and nothing from Pillow.

Input, one dict per page::

    {"page": 1, "width": 1632, "height": 2112,
     "blocks": [{"label": "SectionHeader", "html": "<h1>Pump Manual</h1>", "bbox": [x0, y0, x1, y1],
                 "reading_order": 0, "skipped": False, "error": False}],
     "image": {"mime": "image/png", "data_b64": "..."} or None}

The shapes come from ``surya/recognition/schema.py`` (``PageOCRResult``: ``image_bbox``, ``blocks``;
``BlockOCRResult``: ``label``, ``html``, ``skipped``, ``error``, ``reading_order`` and the
``PolygonBox`` ``bbox``) and ``surya/layout/label.py`` (the labels).

======================================  =======================================================
Label                                   IR block
======================================  =======================================================
``SectionHeader``                       ``heading``, level from the leading ``<hn>`` tag, else 2
``Text``, ``Bibliography``              ``paragraph``
``Form``, ``ChemicalBlock``             ``paragraph`` with ``metadata.original_type`` = the label
``ListGroup``, ``TableOfContents``      one ``list_item`` per ``<li>``; without any, a
                                        ``paragraph``
``Table``                               ``table``: Markdown in ``text``, the HTML in ``html``
``Equation``                            ``formula``, the LaTeX inside ``<math>`` as ``text``
``Code``                                ``code`` (line breaks kept)
``Picture``, ``Figure``, ``Diagram``    ``figure``, the block's text (often empty)
``Caption``                             ``caption``
``Footnote``                            ``footnote``
``PageHeader``, ``PageFooter``          ``page_header``, ``page_footer``
``BlankPage`` and blocks with an error  dropped
anything else                           ``paragraph`` with ``metadata.original_type``
======================================  =======================================================

Surya has no document-title label, so a document only has a level-1 heading when the model wrote an
``<h1>``; none is promoted. Boxes and page sizes are the pixels of the rendered page image.
"""

from __future__ import annotations

from typing import Any

from shimkit import build_ir, first_heading_level, html_to_text, split_list_items, table_to_markdown

_PARAGRAPHS = {"Text", "Bibliography"}
_KEEP_LABEL = {"Form", "ChemicalBlock"}
_LISTS = {"ListGroup", "TableOfContents"}
_FIGURES = {"Picture", "Figure", "Diagram"}
_SAME = {"Caption": "caption", "Footnote": "footnote", "PageHeader": "page_header", "PageFooter": "page_footer"}


def _latex(html: str) -> str:
    """The LaTeX inside the block's ``<math>`` elements (``html_to_text`` writes them as ``$...$``)."""
    parts = []
    for line in html_to_text(html).splitlines():
        line = line.strip()
        if len(line) >= 2 and line.startswith("$") and line.endswith("$"):
            line = line[1:-1].strip()
        if line:
            parts.append(line)
    return "\n".join(parts)


def _blocks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    label = str(raw.get("label") or "")
    html = str(raw.get("html") or "")
    if label == "SectionHeader":
        level = first_heading_level(html) or 2
        return [{"type": "heading", "level": level, "text": html_to_text(html)}]
    if label in _PARAGRAPHS:
        return [{"type": "paragraph", "text": html_to_text(html)}]
    if label in _KEEP_LABEL:
        return [{"type": "paragraph", "text": html_to_text(html), "metadata": {"original_type": label}}]
    if label in _LISTS:
        items = split_list_items(html)
        if items:
            return [{"type": "list_item", "text": item} for item in items]
        return [{"type": "paragraph", "text": html_to_text(html)}]
    if label == "Table":
        block = {"type": "table", "text": table_to_markdown(html) or html_to_text(html)}
        if "<table" in html.lower():
            block["html"] = html.strip()
        return [block]
    if label == "Equation":
        return [{"type": "formula", "text": _latex(html)}]
    if label == "Code":
        return [{"type": "code", "text": html_to_text(html)}]
    if label in _FIGURES:
        return [{"type": "figure", "text": html_to_text(html)}]
    if label in _SAME:
        return [{"type": _SAME[label], "text": html_to_text(html)}]
    return [{"type": "paragraph", "text": html_to_text(html), "metadata": {"original_type": label or "unknown"}}]


def to_ir(
    pages: list[dict[str, Any]], *, source_name: str | None = None, include_page_images: bool = False
) -> dict[str, Any]:
    """Convert reduced Surya page results to a Document IR ``dict``."""
    blocks: list[dict[str, Any]] = []
    ir_pages: list[dict[str, Any]] = []
    for page in sorted(pages, key=lambda p: int(p["page"])):
        number = int(page["page"])
        entry: dict[str, Any] = {"page": number, "width": float(page["width"]), "height": float(page["height"])}
        if include_page_images and page.get("image"):
            entry["image"] = page["image"]
        ir_pages.append(entry)
        ordered = sorted(page.get("blocks", []), key=lambda b: b.get("reading_order") or 0)
        for raw in ordered:
            if raw.get("error") or raw.get("label") == "BlankPage":
                continue
            for block in _blocks(raw):
                block["page"] = number
                if raw.get("bbox") is not None:
                    block["bbox"] = [float(v) for v in raw["bbox"]]
                blocks.append(block)
    return build_ir(blocks, ir_pages, source_name=source_name)
