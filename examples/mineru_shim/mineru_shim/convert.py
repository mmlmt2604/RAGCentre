"""Turn MinerU's flat content list into RAGCentre's Document IR (schema 1.1). This module imports
nothing from MinerU.

``mineru.render.content_list.render_content_list(result.middle_json)`` returns a list of dicts in
reading order (``mineru/render/_internal/content_list/v1.py``). Fields are present only when they
have a value (``table_body``, an equation's ``text``), ``bbox`` is four integers on a 0 to 1000
scale relative to the page with a top-left origin, and ``page_idx`` is 0-based. The result carries
no page sizes, so the engine module reads them from the file and passes them in.

===========================  ===========================================================
Content list item            IR block
===========================  ===========================================================
``text`` with ``text_level`` ``heading``, level 1 to 6
``text``                     ``paragraph``
``list``                     one ``list_item`` per entry of ``list_items``
``index``                    one ``list_item`` per entry (``- `` and indent stripped),
                             ``metadata.original_type`` = ``index``
``equation``                 ``formula`` (LaTeX)
``table``                    a ``caption`` per caption, the ``table`` (Markdown in ``text``,
                             the HTML in ``html``), a ``footnote`` per footnote
``image``, ``chart``         ``figure`` (captions and the analysed ``content``, separated by
                             blank lines), then a ``footnote`` per footnote
``code``                     a ``caption`` per caption, the ``code``, a ``footnote`` per
                             footnote
``header``                   ``page_header``
``footer``, ``page_number``  ``page_footer``
``page_footnote``            ``footnote``
``aside_text`` and others    the item type (RAGCentre keeps it as ``metadata.original_type``)
===========================  ===========================================================

Boxes are scaled to the page's own size (``x * width / 1000``) so that they share the unit of
``pages[].width``: points for a PDF, pixels for an image. For a file whose page size is unknown (an
Office file, HTML) boxes are omitted.
"""

from __future__ import annotations

import re
from typing import Any

from shimkit import build_ir, html_to_text, table_to_markdown

PageSizes = dict[int, tuple[float, float]]

_LINK = re.compile(r"^\[(?P<label>.*)\]\([^)]*\)$")
_SAME = {"header": "page_header", "footer": "page_footer", "page_number": "page_footer", "page_footnote": "footnote"}


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value or [] if str(v).strip()]


def _index_entry(entry: str) -> str:
    text = entry.strip().removeprefix("-").strip()
    link = _LINK.match(text)
    return link.group("label").strip() if link else text


def _scaled_box(bbox: Any, size: tuple[float, float] | None) -> list[float] | None:
    if size is None or not isinstance(bbox, list | tuple) or len(bbox) != 4:
        return None
    width, height = size
    x0, y0, x1, y1 = (float(v) for v in bbox)
    return [x0 * width / 1000, y0 * height / 1000, x1 * width / 1000, y1 * height / 1000]


def _blocks(item: dict[str, Any]) -> list[dict[str, Any]]:
    kind = str(item.get("type") or "text")
    text = str(item.get("text") or "")
    if kind == "text":
        level = item.get("text_level")
        if isinstance(level, int) and not isinstance(level, bool) and level > 0:
            return [{"type": "heading", "level": min(6, level), "text": text}]
        return [{"type": "paragraph", "text": text}]
    if kind == "list":
        return [{"type": "list_item", "text": entry} for entry in _strings(item.get("list_items"))]
    if kind == "index":
        return [
            {"type": "list_item", "text": _index_entry(entry), "metadata": {"original_type": "index"}}
            for entry in _strings(item.get("list_items"))
        ]
    if kind == "equation":
        return [{"type": "formula", "text": text}]
    if kind == "table":
        body = str(item.get("table_body") or "")
        table: dict[str, Any] = {"type": "table", "text": table_to_markdown(body) or html_to_text(body)}
        if body:
            table["html"] = body
        return [
            *({"type": "caption", "text": c} for c in _strings(item.get("table_caption"))),
            table,
            *({"type": "footnote", "text": f} for f in _strings(item.get("table_footnote"))),
        ]
    if kind in ("image", "chart"):
        parts = [*_strings(item.get(f"{kind}_caption")), str(item.get("content") or "").strip()]
        return [
            {"type": "figure", "text": "\n\n".join(p for p in parts if p)},
            *({"type": "footnote", "text": f} for f in _strings(item.get(f"{kind}_footnote"))),
        ]
    if kind == "code":
        return [
            *({"type": "caption", "text": c} for c in _strings(item.get("code_caption"))),
            {"type": "code", "text": str(item.get("code_body") or "")},
            *({"type": "footnote", "text": f} for f in _strings(item.get("code_footnote"))),
        ]
    return [{"type": _SAME.get(kind, kind), "text": text}]  # aside_text and anything new keep their name


def to_ir(
    content_list: list[dict[str, Any]], page_sizes: PageSizes, *, source_name: str | None = None
) -> dict[str, Any]:
    """Convert MinerU's content list to a Document IR ``dict``. ``page_sizes`` maps a 1-based page
    number to ``(width, height)``."""
    blocks: list[dict[str, Any]] = []
    for item in content_list:
        number = int(item.get("page_idx") or 0) + 1
        box = _scaled_box(item.get("bbox"), page_sizes.get(number))
        for block in _blocks(item):
            block["page"] = number
            if box is not None:
                block["bbox"] = list(box)
            blocks.append(block)
    pages = [{"page": n, "width": float(w), "height": float(h)} for n, (w, h) in sorted(page_sizes.items())]
    return build_ir(blocks, pages, source_name=source_name)
