"""Paragraphs, reading order and heading guesses for engines that only return text lines.

EasyOCR, docTR and Tesseract find text lines (or pieces of lines) and read them, but they do not lay
out a page: they give no paragraphs, headings or reading order. :func:`lines_to_ir` builds those from
the geometry alone:

1. :func:`merge_rows` joins pieces of one text line (EasyOCR splits a line at wide gaps) when they
   overlap vertically and the gap between them is small.
2. :func:`group_paragraphs` adds a line to the paragraph above it when it starts close below that
   paragraph's last line, overlaps it horizontally and has about the same height. Paragraphs are
   ordered by their first line, top to bottom then left to right. That is right for one column; on a
   page with columns side by side the paragraphs of the columns interleave (each stays whole).
3. :func:`guess_heading_levels` (opt-in) calls a short paragraph (one or two lines, few words, no
   closing full stop or comma) a heading when its text is clearly taller than the document's body
   text. The tallest heading size is level 1, the next smaller one level 2, and so on.

Standard library only. Boxes are ``(x0, y0, x1, y1)`` with the origin top-left, in any one unit.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from shimkit.ir import build_ir

Box = tuple[float, float, float, float]

HEADING_RATIO = 1.25  # a heading's line is at least this many times as tall as the body text
HEADING_MAX_WORDS = 15


@dataclass(frozen=True)
class Line:
    """One text line (or a piece of one), as an engine read it."""

    text: str
    bbox: Box
    confidence: float | None = None

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


def _union(boxes: Iterable[Box]) -> Box:
    x0s, y0s, x1s, y1s = zip(*boxes, strict=True)
    return (min(x0s), min(y0s), max(x1s), max(y1s))


def _mean(values: Iterable[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else None


_HYPHENATED = re.compile(r"[^\W\d_]-$")


def join_lines(lines: Iterable[str]) -> str:
    """The lines of a paragraph as one string: joined with a space, except that a word hyphenated
    at the end of a line (``"exam-"`` then ``"ple"``) is joined back together."""
    out = ""
    for line in (raw.strip() for raw in lines):
        if not line:
            continue
        if out and _HYPHENATED.search(out) and line[:1].islower():
            out = out[:-1] + line
        else:
            out = f"{out} {line}" if out else line
    return out


@dataclass
class Paragraph:
    lines: list[Line] = field(default_factory=list)

    @property
    def bbox(self) -> Box:
        return _union(line.bbox for line in self.lines)

    @property
    def text(self) -> str:
        return join_lines(line.text for line in self.lines)

    @property
    def line_height(self) -> float:
        return float(statistics.median(line.height for line in self.lines))

    @property
    def confidence(self) -> float | None:
        return _mean(line.confidence for line in self.lines)


def _vertical_overlap(a: Box, b: Box) -> float:
    return min(a[3], b[3]) - max(a[1], b[1])


def merge_rows(pieces: Iterable[Line], *, gap: float = 1.0) -> list[Line]:
    """Join pieces of the same text line, left to right.

    A piece continues a row when it overlaps the row's last piece vertically by at least half the
    smaller height and starts at most ``gap`` times the taller height to its right. Pieces further
    apart (the columns of a table, say) stay separate lines. The result is sorted top to bottom,
    then left to right; a merged line's confidence is the mean of its pieces'."""
    rows: list[list[Line]] = []
    for piece in sorted(pieces, key=lambda p: (p.bbox[0], p.bbox[1])):
        best: list[Line] | None = None
        best_distance = 0.0
        for row in rows:
            last = row[-1]
            shorter = min(last.height, piece.height)
            taller = max(last.height, piece.height)
            if shorter <= 0:
                continue
            distance = piece.bbox[0] - last.bbox[2]
            if _vertical_overlap(last.bbox, piece.bbox) < 0.5 * shorter:
                continue
            if not -0.5 * shorter <= distance <= gap * taller:
                continue
            if best is None or distance < best_distance:
                best, best_distance = row, distance
        if best is None:
            rows.append([piece])
        else:
            best.append(piece)
    lines = [
        Line(
            " ".join(p.text.strip() for p in row),
            _union(p.bbox for p in row),
            _mean(p.confidence for p in row),
        )
        for row in rows
    ]
    return sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))


def group_paragraphs(
    lines: Iterable[Line],
    *,
    gap: float = 0.8,
    height_tolerance: float = 0.3,
    is_heading: Callable[[Line], bool] | None = None,
) -> list[Paragraph]:
    """Group lines into paragraphs (see the module docstring). A line joins a paragraph when the
    space between them is at most ``gap`` times the taller line's height, their horizontal extents
    overlap, and their heights differ by at most ``height_tolerance`` of the taller one (so a title
    does not run into the text under it).

    The height tolerance alone cannot keep a heading apart: it is looser than the ratio that makes a
    line a heading, so a heading up to about 1.4 times as tall as the text beside it would join that
    text. ``is_heading`` closes the gap: a line for which it is true never joins a paragraph whose
    last line it is false for, and the other way round. Lines that are both headings still join, so
    a heading of two lines stays one block."""
    paragraphs: list[Paragraph] = []
    for line in sorted(lines, key=lambda item: (item.bbox[1], item.bbox[0])):
        best: Paragraph | None = None
        best_space = 0.0
        for paragraph in paragraphs:
            last = paragraph.lines[-1]
            taller = max(last.height, line.height)
            if taller <= 0:
                continue
            if is_heading is not None and is_heading(last) != is_heading(line):
                continue
            space = line.bbox[1] - last.bbox[3]
            if not -0.5 * taller <= space <= gap * taller:
                continue
            if min(last.bbox[2], line.bbox[2]) <= max(last.bbox[0], line.bbox[0]):
                continue  # side by side, not one above the other
            if abs(last.height - line.height) > height_tolerance * taller:
                continue
            if best is None or space < best_space:
                best, best_space = paragraph, space
        if best is None:
            paragraphs.append(Paragraph([line]))
        else:
            best.lines.append(line)
    return sorted(paragraphs, key=lambda p: (p.lines[0].bbox[1], p.lines[0].bbox[0]))


def _looks_like_heading(text: str, height: float, body: float, *, ratio: float, max_words: int) -> bool:
    """Few words, no closing ``.``, ``,`` or ``;``, a letter in it, and ``ratio`` times as tall as ``body``."""
    if len(text.split()) > max_words or text[-1:] in (".", ",", ";") or not any(c.isalpha() for c in text):
        return False
    return height >= ratio * body


def guess_heading_levels(
    paragraphs: Sequence[Paragraph],
    *,
    ratio: float = HEADING_RATIO,
    max_words: int = HEADING_MAX_WORDS,
    max_levels: int = 4,
) -> list[int | None]:
    """A heading level (1 to ``max_levels``) or ``None`` for each paragraph.

    The body height is the median height of every line in ``paragraphs``. A paragraph is a heading
    when it has at most two lines and ``max_words`` words, contains a letter, does not end with
    ``.``, ``,`` or ``;``, and its line height is at least ``ratio`` times the body height. Heading
    heights within about 12% of each other share a level; the tallest group is level 1."""
    heights = [line.height for p in paragraphs for line in p.lines if line.height > 0]
    if not heights:
        return [None] * len(paragraphs)
    body = statistics.median(heights)
    candidates: dict[int, float] = {}
    for index, paragraph in enumerate(paragraphs):
        if len(paragraph.lines) > 2:
            continue
        height = paragraph.line_height
        if _looks_like_heading(paragraph.text, height, body, ratio=ratio, max_words=max_words):
            candidates[index] = height
    levels: dict[float, int] = {}
    level, top = 0, 0.0
    for size in sorted(set(candidates.values()), reverse=True):
        if level == 0 or size < top / 1.12:
            level, top = level + 1, size
        levels[size] = min(level, max_levels)
    return [levels[candidates[i]] if i in candidates else None for i in range(len(paragraphs))]


def _heading_test(rows: Sequence[Line]) -> Callable[[Line], bool] | None:
    """The test :func:`group_paragraphs` takes as ``is_heading``: a line that :func:`guess_heading_levels` would
    call a heading, judged against the median height of all ``rows`` of the document (``None`` without a height)."""
    heights = [row.height for row in rows if row.height > 0]
    if not heights:
        return None
    body = statistics.median(heights)

    def is_heading(row: Line) -> bool:
        return _looks_like_heading(row.text, row.height, body, ratio=HEADING_RATIO, max_words=HEADING_MAX_WORDS)

    return is_heading


def _confidence(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def lines_to_ir(
    pages: Sequence[Mapping[str, Any]],
    *,
    source_name: str | None = None,
    paragraphs: bool = True,
    headings: bool = False,
    min_confidence: float = 0.0,
    include_page_images: bool = False,
) -> dict[str, Any]:
    """Build a Document IR from pages of text lines. Each page is::

        {"page": 1, "width": 1700, "height": 2200,
         "lines": [{"text": "...", "bbox": [x0, y0, x1, y1], "confidence": 0.98}],
         "image": {"mime": "image/png", "data_b64": "..."} or None}

    Lines with blank text, or a ``confidence`` below ``min_confidence`` (0 to 1), are dropped. With
    ``paragraphs`` lines are grouped into ``paragraph`` blocks; without it each line is its own block.
    With ``headings`` the guess of :func:`guess_heading_levels` (over the whole document) turns some
    blocks into ``heading`` blocks. A block's ``metadata.confidence`` is the mean of its lines'."""
    ir_pages: list[dict[str, Any]] = []
    page_rows: list[tuple[int, list[Line]]] = []
    for page in sorted(pages, key=lambda p: int(p["page"])):
        number = int(page["page"])
        entry: dict[str, Any] = {"page": number, "width": float(page["width"]), "height": float(page["height"])}
        if include_page_images and page.get("image"):
            entry["image"] = page["image"]
        ir_pages.append(entry)
        pieces = []
        for raw in page.get("lines", []):
            text = str(raw.get("text") or "").strip()
            confidence = _confidence(raw.get("confidence"))
            if not text or (confidence is not None and confidence < min_confidence):
                continue
            x0, y0, x1, y1 = (float(v) for v in raw["bbox"])
            pieces.append(Line(text, (x0, y0, x1, y1), confidence))
        page_rows.append((number, merge_rows(pieces)))
    is_heading = _heading_test([row for _, rows in page_rows for row in rows]) if headings else None
    laid_out: list[tuple[int, Paragraph]] = []
    for number, rows in page_rows:
        grouped = group_paragraphs(rows, is_heading=is_heading) if paragraphs else [Paragraph([row]) for row in rows]
        laid_out.extend((number, paragraph) for paragraph in grouped)
    levels = guess_heading_levels([p for _, p in laid_out]) if headings else [None] * len(laid_out)
    blocks: list[dict[str, Any]] = []
    for (number, paragraph), level in zip(laid_out, levels, strict=True):
        block: dict[str, Any] = (
            {"type": "heading", "level": level, "text": paragraph.text}
            if level
            else {"type": "paragraph", "text": paragraph.text}
        )
        block["page"] = number
        block["bbox"] = list(paragraph.bbox)
        confidence = paragraph.confidence
        if confidence is not None:
            block["metadata"] = {"confidence": round(confidence, 4)}
        blocks.append(block)
    return build_ir(blocks, ir_pages, source_name=source_name)
