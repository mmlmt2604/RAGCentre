"""Turn PP-StructureV3 page results (reduced to plain dicts by ``engine.py``) into RAGCentre's
Document IR (schema 1.0). This module imports nothing from PaddleOCR.

Input, one dict per page::

    {"page": 1, "width": 1224, "height": 1584,
     "blocks": [{"label": "paragraph_title", "content": "...", "bbox": [x0, y0, x1, y1],
                 "order": 3, "title_level": 1}]}

The shapes come from ``paddlex/inference/pipelines/layout_parsing/result_v2.py`` (page fields) and
``layout_objects.py`` (``LayoutBlock``: ``label``, ``content``, ``bbox``, ``order_index``);
``title_level`` is **not** set by PP-StructureV3 (only PaddleOCR-VL assigns it); the engine module
applies PaddleX's ``assign_levels_to_parsing_res`` when the ``heading_levels`` option is on. It
sets it on ``paragraph_title`` blocks only, and it starts at 1.

=========================================  ====================================================
Label                                      IR block
=========================================  ====================================================
``doc_title``                              ``heading`` level 1
``paragraph_title``, ``abstract_title``,   ``heading`` level ``title_level + 1`` (2 to 6), or 2
``reference_title``, ``content_title``     when there is no ``title_level``
``text``, ``abstract``, ``content``,       ``paragraph``
``reference``
``table``                                  ``table``: Markdown in ``text``, HTML in ``html``
``formula``                                ``formula``
``algorithm``                              ``code``
``image``, ``figure``, ``chart``,          ``figure``, with the block's content as ``text``
``flowchart``, ``seal``
``table_title``, ``chart_title``,          ``caption``
``figure_title``,
``figure_table_chart_title``
``footnote``                               ``footnote``
``header``                                 ``page_header``
``footer``, ``number``                     ``page_footer``
``header_image``, ``footer_image``,        dropped
``formula_number``
anything else (``aside_text``, ...)        the label as the type (RAGCentre keeps it as
                                           ``metadata.original_type``)
=========================================  ====================================================

Boxes and page sizes are the engine's pixels (top-left origin) and are passed through unchanged.
"""

from __future__ import annotations

from typing import Any

from shimkit import build_ir, html_to_text, table_to_markdown

_TITLES = {"paragraph_title", "abstract_title", "reference_title", "content_title"}
_PARAGRAPHS = {"text", "abstract", "content", "reference"}
_FIGURES = {"image", "figure", "chart", "flowchart", "seal"}
_CAPTIONS = {"table_title", "chart_title", "figure_title", "figure_table_chart_title"}
_FOOTERS = {"footer", "number"}
_DROPPED = {"header_image", "footer_image", "formula_number"}


def _heading_level(title_level: Any) -> int:
    if isinstance(title_level, int) and not isinstance(title_level, bool):
        return min(6, max(2, title_level + 1))
    return 2


def _table(content: str) -> dict[str, Any]:
    start = content.lower().find("<table")
    if start < 0:  # table recognition was off: the content is plain text
        return {"type": "table", "text": content.strip()}
    end = content.lower().rfind("</table>")
    html = content[start : end + len("</table>")] if end > start else content[start:]  # drop <html><body>
    return {"type": "table", "text": table_to_markdown(html) or html_to_text(html), "html": html}


def _block(raw: dict[str, Any]) -> dict[str, Any] | None:
    label = str(raw.get("label") or "text")
    content = str(raw.get("content") or "").strip()
    block: dict[str, Any]
    if label in _DROPPED:
        return None
    if label == "doc_title":
        block = {"type": "heading", "level": 1, "text": content}
    elif label in _TITLES:
        block = {"type": "heading", "level": _heading_level(raw.get("title_level")), "text": content}
    elif label in _PARAGRAPHS:
        block = {"type": "paragraph", "text": content}
    elif label == "table":
        block = _table(content)
    elif label == "formula":
        block = {"type": "formula", "text": content}
    elif label == "algorithm":
        block = {"type": "code", "text": content}
    elif label in _FIGURES:
        block = {"type": "figure", "text": content}
    elif label in _CAPTIONS:
        block = {"type": "caption", "text": content}
    elif label == "footnote":
        block = {"type": "footnote", "text": content}
    elif label == "header":
        block = {"type": "page_header", "text": content}
    elif label in _FOOTERS:
        block = {"type": "page_footer", "text": content}
    else:
        block = {"type": label, "text": content}  # kept as metadata.original_type by RAGCentre
    return block


def to_ir(pages: list[dict[str, Any]], *, source_name: str | None = None) -> dict[str, Any]:
    """Convert reduced PP-StructureV3 page results to a Document IR ``dict``."""
    blocks: list[dict[str, Any]] = []
    ir_pages: list[dict[str, Any]] = []
    for page in sorted(pages, key=lambda p: int(p["page"])):
        number = int(page["page"])
        ir_pages.append({"page": number, "width": float(page["width"]), "height": float(page["height"])})
        for raw in page.get("blocks", []):
            block = _block(raw)
            if block is None:
                continue
            block["page"] = number
            if raw.get("bbox") is not None:
                block["bbox"] = [float(v) for v in raw["bbox"]]
            blocks.append(block)
    return build_ir(blocks, ir_pages, source_name=source_name)
