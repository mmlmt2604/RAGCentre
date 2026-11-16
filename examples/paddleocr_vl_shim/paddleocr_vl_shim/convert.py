"""Turn PaddleOCR-VL page results (reduced to plain dicts by ``engine.py``) into RAGCentre's Document
IR (schema 1.1). This module imports nothing from PaddleOCR.

Input, one dict per page::

    {"page": 1, "width": 1224, "height": 1584,
     "blocks": [{"label": "paragraph_title", "content": "...", "bbox": [x0, y0, x1, y1],
                 "title_level": 1}]}

Blocks arrive in the engine's reading order: PaddleOCR-VL has no ``order_index`` (PP-StructureV3 has
one); the layout model returns its regions already ordered, and ``parsing_res_list`` keeps that order.

Where the labels come from (PaddleOCR 3.7.0, PaddleX 3.7.2):

* ``label_list`` in the ``inference.yml`` of the layout models ``PP-DocLayoutV2`` (pipeline ``v1``)
  and ``PP-DocLayoutV3`` (``v1.5`` and ``v1.6``): the same 25 labels. All 25 are in the table.
* ``PaddleOCRVL.predict(..., use_layout_detection=False)`` skips the layout model and labels the
  whole page with ``prompt_label`` (``paddleocr_vl/pipeline.py``: ``"label": prompt_label.lower()``,
  one of ``ocr``, ``formula``, ``table``, ``chart``, ``spotting``, ``seal``); ``ocr`` and ``spotting``
  are labels that the layout model never emits, so they are in the table too.
* ``title_level`` is set by ``restructure_pages`` (``paddleocr_vl/pipeline.py``,
  ``layout_parsing/title_level.py``), not by ``predict``; only ``paragraph_title`` blocks get one, and
  it starts at 1. ``doc_title`` never has one.

=========================================  ====================================================
Label                                      IR block
=========================================  ====================================================
``doc_title``                              ``heading`` level 1
``paragraph_title``                        ``heading`` level ``title_level + 1`` (2 to 6), or 2
                                           when there is no ``title_level``
``text``, ``vertical_text``,               ``paragraph`` (PaddleOCR keeps a leading "Abstract" or
``abstract``, ``content``, ``reference``,  "References" in the text)
``reference_content``, ``ocr``,
``spotting``
``table``                                  ``table``: Markdown in ``text``, HTML in ``html``
``display_formula``, ``inline_formula``,   ``formula``
``formula``
``algorithm``                              ``code``
``image``, ``chart``, ``seal``             ``figure``, with the block's content as ``text`` (empty
                                           unless chart, seal or image OCR is on)
``figure_title``                           ``caption`` (PaddleOCR-VL uses this label for table,
                                           chart and figure captions alike)
``footnote``, ``vision_footnote``          ``footnote``
``header``                                 ``page_header``
``footer``, ``number``                     ``page_footer`` (``number`` is the page number)
``aside_text``                             ``aside_text`` (RAGCentre keeps it as
                                           ``metadata.original_type``): margin notes and stamps
``header_image``, ``footer_image``         dropped: pictures in the page margins
``formula_number``                         dropped: the "(3)" beside a display formula. The engine
                                           merges it into the formula only when it renders Markdown
anything else                              the label as the type (RAGCentre keeps it as
                                           ``metadata.original_type``)
=========================================  ====================================================

Boxes and page sizes are the engine's pixels (top-left origin) and are passed through unchanged.
"""

from __future__ import annotations

from typing import Any

from shimkit import build_ir, html_to_text, table_to_markdown

# Every label the pipeline can emit (see the module docstring). ``test_every_label_is_in_the_table``
# checks that each one is handled on purpose, so a label added here must also be mapped below.
LAYOUT_LABELS = (
    "abstract", "algorithm", "aside_text", "chart", "content", "display_formula", "doc_title",
    "figure_title", "footer", "footer_image", "footnote", "formula_number", "header", "header_image",
    "image", "inline_formula", "number", "paragraph_title", "reference", "reference_content", "seal",
    "table", "text", "vertical_text", "vision_footnote",
)  # fmt: skip
PROMPT_LABELS = ("ocr", "formula", "table", "chart", "spotting", "seal")

_PARAGRAPHS = {"text", "vertical_text", "abstract", "content", "reference", "reference_content", "ocr", "spotting"}
_FORMULAS = {"display_formula", "inline_formula", "formula"}
_FIGURES = {"image", "chart", "seal"}
_FOOTNOTES = {"footnote", "vision_footnote"}
_FOOTERS = {"footer", "number"}
_DROPPED = {"header_image", "footer_image", "formula_number"}


def _heading_level(title_level: Any) -> int:
    if isinstance(title_level, int) and not isinstance(title_level, bool):
        return min(6, max(2, title_level + 1))
    return 2


def _table(content: str) -> dict[str, Any]:
    start = content.lower().find("<table")
    if start < 0:  # no HTML: the engine could not convert the model's table tokens, so this is its text
        return {"type": "table", "text": content.strip()}
    end = content.lower().rfind("</table>")
    html = content[start : end + len("</table>")] if end > start else content[start:]
    return {"type": "table", "text": table_to_markdown(html) or html_to_text(html), "html": html}


def _block(raw: dict[str, Any]) -> dict[str, Any] | None:
    label = str(raw.get("label") or "text")
    content = str(raw.get("content") or "").strip()
    if label in _DROPPED:
        return None
    if label == "doc_title":
        return {"type": "heading", "level": 1, "text": content}
    if label == "paragraph_title":
        return {"type": "heading", "level": _heading_level(raw.get("title_level")), "text": content}
    if label in _PARAGRAPHS:
        return {"type": "paragraph", "text": content}
    if label == "table":
        return _table(content)
    if label in _FORMULAS:
        return {"type": "formula", "text": content}
    if label == "algorithm":
        return {"type": "code", "text": content}
    if label in _FIGURES:
        return {"type": "figure", "text": content}
    if label == "figure_title":
        return {"type": "caption", "text": content}
    if label in _FOOTNOTES:
        return {"type": "footnote", "text": content}
    if label == "header":
        return {"type": "page_header", "text": content}
    if label in _FOOTERS:
        return {"type": "page_footer", "text": content}
    return {"type": label, "text": content}  # aside_text, or a label a newer model adds


def to_ir(pages: list[dict[str, Any]], *, source_name: str | None = None) -> dict[str, Any]:
    """Convert reduced PaddleOCR-VL page results to a Document IR ``dict``."""
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
            if raw.get("spans"):
                block["spans"] = raw["spans"]
            blocks.append(block)
    return build_ir(blocks, ir_pages, source_name=source_name)
