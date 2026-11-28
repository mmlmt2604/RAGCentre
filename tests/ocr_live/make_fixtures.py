"""Build the OCR verification fixtures and ``truth.json``, deterministically.

    uv run --with pillow --with reportlab python tests/ocr_live/make_fixtures.py [--out DIR]

Writes ``DIR/fixtures/*`` and ``DIR/truth.json`` (``DIR`` defaults to this folder). The output is committed, so a
test run needs neither Pillow, reportlab nor fonts. Pillow and reportlab are not project dependencies. Needs
macOS system fonts (Times New Roman, Arial, Courier New, Hiragino Sans GB).

Ground truth is derived from the build, never from an engine:

* Image line boxes are the ink bounding box of each line, measured on that line drawn alone and pushed through
  the same transform (rotation, skew, warp) as the page. They are in pixels of the stored image.
* PDF boxes are in points, origin top-left, computed from font metrics, so they are good to about 2 pt.
* ``tokens`` of a page are the words a reader should recover. Formulas, code and page furniture are left out
  of ``tokens`` and described in ``blocks`` and ``extras``.

The build checks itself (size under 200 KB, ink inside line boxes, token sets that must differ do differ) and
fails loudly, so a bad fixture never reaches ``truth.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import sys
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont  # noqa: E402
from predicates import recall, tokenize  # noqa: E402  the tokenizer recall uses, so truth and results agree
from reportlab import rl_config  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

rl_config.useA85 = 0  # raw Flate for images: ASCII85 would make them a quarter larger

SEED = 20261128
MAX_BYTES = 200 * 1024
EXIT_NO_FONTS = 3  # the self-test skips, rather than fails, when the macOS fonts are absent

SYSTEM = Path("/System/Library/Fonts")
TIMES = SYSTEM / "Supplemental" / "Times New Roman.ttf"
ARIAL = SYSTEM / "Supplemental" / "Arial.ttf"
ARIAL_BOLD = SYSTEM / "Supplemental" / "Arial Bold.ttf"
COURIER = SYSTEM / "Supplemental" / "Courier New.ttf"
CHINESE = SYSTEM / "Hiragino Sans GB.ttc"  # simplified Chinese

# --- text ---------------------------------------------------------------------------------------

ENGLISH = (
    "The quarterly inspection of the harbour warehouse began on a windy Tuesday morning. Inspectors measured "
    "the loading docks, counted forty-two pallets of ceramic tiles, and recorded every serial number in a "
    "battered blue notebook. By noon the crew had finished the eastern wing, while the cold storage room "
    "still waited for its turn. The final report will list seventeen defects, from a cracked window to a "
    "missing fire extinguisher."
)
GERMAN = (
    "Die Prüfung der Lagerhalle im Hafen begann an einem windigen Dienstagmorgen. Die Prüfer maßen die "
    "Laderampen, zählten zweiundvierzig Paletten mit Keramikfliesen und notierten jede Seriennummer in einem "
    "abgegriffenen blauen Notizbuch. Bis zum Mittag war der Ostflügel fertig, während der Kühlraum noch warten "
    "musste. Der Abschlussbericht nennt siebzehn Mängel, vom gesprungenen Fenster bis zum fehlenden Feuerlöscher."
)
CHINESE_TEXT = (
    "今天上午，检查人员来到港口仓库，清点了四十二箱陶瓷砖，并把每一个编号都写进了蓝色的笔记本。"
    "中午之前，他们完成了东侧大厅的检查，冷藏室还在等待。最终报告将列出十七处缺陷，从破裂的窗户到缺失的灭火器。"
)
# Pages of scan_multipage.pdf. Each has its own vocabulary, so a page window can be told from the whole file.
MULTIPAGE = [
    "Page one describes the northern jetty. Gulls nest along the rusted railings, and the morning ferry "
    "unloads crates of oranges beside a stack of folded tarpaulins.",
    "Page two covers the timber yard. Cedar boards dry under a long awning while a forklift driver "
    "sharpens pencils and sorts invoices into green folders.",
    "Page three visits the paint store. Sealed drums of crimson and turquoise enamel line the shelves, "
    "each marked with a brass tag and a faded expiry date.",
    "Page four inspects the generator room. Copper cables run beneath a humming transformer, and a "
    "laminated checklist hangs from a magnet near the door.",
    "Page five closes the walk around the office. A kettle whistles in the corner, two clocks disagree "
    "by several minutes, and the visitor book waits for a signature.",
]
SMALL_PAGES = [  # frames of multi_frame.tiff
    "Frame one shows a ladder leaning against a whitewashed wall.",
    "Frame two shows a bucket of nails beside a coil of yellow rope.",
    "Frame three shows a lantern hanging from a hook above the door.",
]
TABLE = [["Area", "Pallets", "Change"], ["Docks", "120", "4.5"], ["Wing", "95", "3.1"], ["Yard", "210", "7.8"]]
SIGN = ["LOADING DOCK TWO CLOSED", "FRIDAY FOR TILE REPAIRS"]  # picture of text in mixed_embedded_image.pdf
CHART_TITLE = "Stock by zone, pallets"
CHART_LABELS = ["Docks", "Wing", "Yard", "Cold room"]
CHART_VALUES = [120, 95, 210, 64]
NO_LINE_START = set("，。、；：！？）》」』,.;:!?)")


# --- what a build produces ----------------------------------------------------------------------


@dataclass
class Built:
    """One fixture: its bytes and the truth that goes in ``truth.json``."""

    data: bytes
    kind: str  # image | pdf | office | text
    units: str  # px | pt | none
    language: str
    pages: list[dict[str, Any]]
    extras: dict[str, Any] = field(default_factory=dict)


def line_truth(text: str, bbox: Sequence[float]) -> dict[str, Any]:
    return {"text": text, "tokens": tokenize(text), "bbox": [round(float(v), 2) for v in bbox]}


def block_truth(
    kind: str, text: str, bbox: Sequence[float], level: int | None = None, html: bool = False
) -> dict[str, Any]:
    out: dict[str, Any] = {"type": kind, "text": text, "bbox": [round(float(v), 2) for v in bbox]}
    if level is not None:
        out["level"] = level
    if html:
        out["html"] = True
    return out


def union(boxes: Sequence[Sequence[float]]) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def page_truth(
    page: int,
    size: tuple[float, float],
    lines: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    tokens: list[str] | None = None,
) -> dict[str, Any]:
    body = tokens if tokens is not None else [t for ln in lines for t in ln["tokens"]]
    return {"page": page, "width": size[0], "height": size[1], "tokens": body, "lines": lines, "blocks": blocks}


def image_bytes(img: Image.Image, fmt: str, **options: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **options)
    return buf.getvalue()


def png_bytes(img: Image.Image) -> bytes:
    return image_bytes(img, "PNG", compress_level=9)


# --- text on a raster page ----------------------------------------------------------------------


def font(path: Path, size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size, index=index)


def wrap(text: str, fnt: ImageFont.FreeTypeFont, width: float) -> list[str]:
    """Greedy word wrap for Latin text; breaks between characters for Chinese."""
    lines: list[str] = []
    cur = ""
    if " " in text:
        for word in text.split():
            trial = f"{cur} {word}".strip()
            if cur and fnt.getlength(trial) > width:
                lines.append(cur)
                cur = word
            else:
                cur = trial
    else:
        for ch in text:
            if cur and fnt.getlength(cur + ch) > width and ch not in NO_LINE_START:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
    return [*lines, cur] if cur else lines


def ink_box(layer: Image.Image) -> list[int]:
    """Bounding box of the ink (value above 32) on a 0-background layer."""
    box = layer.point(lambda v: 255 if v > 32 else 0).getbbox()
    if box is None:
        raise ValueError("a line left no ink")
    return list(box)


def render_lines(
    size: tuple[int, int],
    lines: Sequence[str],
    fnt: ImageFont.FreeTypeFont,
    *,
    x: int,
    y: int,
    leading: int,
    transform: Callable[[Image.Image], Image.Image] | None = None,
) -> tuple[Image.Image, list[dict[str, Any]]]:
    """Black text on white. Each line is drawn alone, transformed, and measured, then merged into the page.

    The page is the merged ink, so a line box is exactly the ink of that line in the stored image."""
    ink: Image.Image | None = None
    truth: list[dict[str, Any]] = []
    for i, text in enumerate(lines):
        layer = Image.new("L", size, 0)
        ImageDraw.Draw(layer).text((x, y + i * leading), text, font=fnt, fill=255)
        layer = transform(layer) if transform else layer
        truth.append(line_truth(text, ink_box(layer)))
        ink = layer if ink is None else ImageChops.lighter(ink, layer)
    assert ink is not None
    return ImageChops.invert(ink), truth


def paragraph_blocks(lines: list[dict[str, Any]], kind: str = "paragraph") -> list[dict[str, Any]]:
    return [block_truth(kind, " ".join(ln["text"] for ln in lines), union([ln["bbox"] for ln in lines]))]


def image_built(
    img_bytes: bytes, size: tuple[int, int], lines: list[dict[str, Any]], language: str, **extras: Any
) -> Built:
    return Built(img_bytes, "image", "px", language, [page_truth(1, size, lines, paragraph_blocks(lines))], extras)


# --- the scan family ----------------------------------------------------------------------------

SCAN_SIZE = (1400, 640)  # 300 dpi: 4.7 x 2.1 inch of 11 pt text


def clean_page(
    text: str,
    fnt: ImageFont.FreeTypeFont,
    transform: Callable[[Image.Image], Image.Image] | None = None,
    size: tuple[int, int] = SCAN_SIZE,
    margin: int = 100,
    leading: int = 66,
):
    lines = wrap(text, fnt, size[0] - 2 * margin)
    return render_lines(size, lines, fnt, x=margin, y=margin - 10, leading=leading, transform=transform)


def solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Gauss-Jordan with partial pivoting, for the 8 unknowns of a perspective transform."""
    n = len(rhs)
    rows = [[*matrix[i], rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(rows[r][col]))
        rows[col], rows[pivot] = rows[pivot], rows[col]
        rows[col] = [v / rows[col][col] for v in rows[col]]
        for r in range(n):
            if r != col:
                factor = rows[r][col]
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[col], strict=True)]
    return [rows[i][n] for i in range(n)]


def perspective(size_out: tuple[int, int], quad: Sequence[tuple[float, float]], size_in: tuple[int, int]):
    """A transform that puts the input rectangle on ``quad`` (TL, TR, BR, BL) of an output of ``size_out``."""
    w, h = size_in
    rect = [(0, 0), (w, 0), (w, h), (0, h)]
    matrix: list[list[float]] = []
    rhs: list[float] = []
    for (x, y), (u, v) in zip(quad, rect, strict=True):  # output (x, y) reads input (u, v)
        matrix.append([x, y, 1, 0, 0, 0, -x * u, -y * u])
        matrix.append([0, 0, 0, x, y, 1, -x * v, -y * v])
        rhs += [u, v]
    coeffs = solve(matrix, rhs)
    return lambda im: im.transform(size_out, Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BICUBIC, fillcolor=0)


def noisy(page: Image.Image, rng: random.Random) -> Image.Image:
    """Blur, low contrast (ink 110, paper 210), 3% speckle, 32 grey levels (so that it fits in 200 KB)."""
    img = page.filter(ImageFilter.GaussianBlur(1.6)).point(lambda v: 110 + v * 100 // 255)
    px = img.load()
    w, h = img.size
    for _ in range(w * h // 33):
        x, y = rng.randrange(w), rng.randrange(h)
        px[x, y] = max(0, min(255, px[x, y] + rng.randint(-70, 70)))
    return img.point(lambda v: v // 8 * 8)


def bilevel(page: Image.Image) -> Image.Image:
    return page.point(lambda v: 255 if v >= 128 else 0).convert("1", dither=Image.Dither.NONE)


def build_scans(rng: random.Random) -> dict[str, Built]:
    out: dict[str, Built] = {}
    times = font(TIMES, 46)
    page, lines = clean_page(ENGLISH, times)
    size = page.size

    def add(
        name: str, img: Image.Image, data: bytes, lines: list[dict[str, Any]], language: str = "en", **extras: Any
    ) -> None:
        out[name] = image_built(data, img.size, lines, language, **extras)

    add("scan_clean.png", page, png_bytes(page), lines, source="Times New Roman 46 px, 300 dpi")
    add("scan_clean.jpg", page, image_bytes(page, "JPEG", quality=85, optimize=True), lines)
    add("scan_clean.webp", page, image_bytes(page, "WEBP", quality=90, method=6), lines)
    add(
        "scan_clean.gif",
        page,
        image_bytes(page, "GIF"),
        lines,
        note="GIF support differs by engine; verify the engine-specific media contract",
    )
    add("scan_clean.bmp", page, image_bytes(bilevel(page), "BMP"), lines, note="1-bit, so that it fits in 200 KB")
    add(
        "scan_noisy.png",
        page,
        png_bytes(noisy(page, rng)),
        lines,
        note="same text and boxes as scan_clean.png, blurred, low contrast, speckled",
    )

    for name, transform, note in [
        (
            "scan_rotated90.png",
            lambda im: im.transpose(Image.Transpose.ROTATE_90),
            "rotated 90 degrees counter-clockwise",
        ),
        ("scan_upside_down.png", lambda im: im.transpose(Image.Transpose.ROTATE_180), "rotated 180 degrees"),
        (
            "scan_skew12.png",
            lambda im: im.rotate(12, Image.Resampling.BICUBIC, expand=True, fillcolor=0),
            "tilted 12 degrees counter-clockwise; line boxes are the axis-aligned box of the tilted line",
        ),
        (
            "scan_warped.png",
            perspective((1500, 800), [(250, 60), (1260, 10), (1480, 740), (20, 700)], size),
            "perspective warp, as a photographed page; line boxes are the axis-aligned box of the warped line",
        ),
    ]:
        warped, wlines = clean_page(ENGLISH, times, transform)
        add(name, warped, png_bytes(warped), wlines, note=note)

    page_de, lines_de = clean_page(GERMAN, times)
    add(
        "scan_german.png",
        page_de,
        png_bytes(page_de),
        lines_de,
        "de",
        umlaut_words=[t for ln in lines_de for t in ln["tokens"] if any(c in t for c in "äöüß")],
    )
    page_zh, lines_zh = clean_page(CHINESE_TEXT, font(CHINESE, 46), leading=72)
    add("scan_chinese.png", page_zh, png_bytes(page_zh), lines_zh, "zh", source="Hiragino Sans GB 46 px")
    return out


# --- table, large page, multi-frame TIFF --------------------------------------------------------

LARGE_EXTRA = (
    "Later the supervisor walked the yard with a clipboard, checking each stack of timber against the "
    "delivery notes. Rain began after lunch, so the tarpaulins were pulled over the paint drums, and the "
    "last forklift was parked beside the gate before the evening shift arrived."
)


def build_table_image() -> Built:
    size, (x0, y0), col_w, row_h = (1400, 560), (100, 100), 400, 90
    fnt = font(TIMES, 44)
    ink = Image.new("L", size, 0)
    draw = ImageDraw.Draw(ink)
    lines: list[dict[str, Any]] = []
    for r, row in enumerate(TABLE):
        for c, cell in enumerate(row):
            layer = Image.new("L", size, 0)
            ImageDraw.Draw(layer).text((x0 + c * col_w + 30, y0 + r * row_h + 20), cell, font=fnt, fill=255)
            lines.append(line_truth(cell, ink_box(layer)))
            ink = ImageChops.lighter(ink, layer)
    draw = ImageDraw.Draw(ink)
    for r in range(len(TABLE) + 1):
        draw.line([(x0, y0 + r * row_h), (x0 + 3 * col_w, y0 + r * row_h)], fill=255, width=4)
    for c in range(4):
        draw.line([(x0 + c * col_w, y0), (x0 + c * col_w, y0 + 4 * row_h)], fill=255, width=4)
    page = ImageChops.invert(ink)
    table = block_truth(
        "table", "\n".join(" | ".join(r) for r in TABLE), [x0, y0, x0 + 3 * col_w, y0 + 4 * row_h], html=True
    )
    return Built(
        png_bytes(page), "image", "px", "en", [page_truth(1, size, lines, [table])],
        {"table": {"rows": 4, "cols": 3, "cells": TABLE, "ruled": True}},
    )  # fmt: skip


def build_large_page() -> Built:
    size = (3508, 4961)  # A3 at 300 dpi
    fnt = font(TIMES, 67)
    paragraphs = [wrap(ENGLISH, fnt, size[0] - 600), wrap(LARGE_EXTRA, fnt, size[0] - 600)]
    ink: Image.Image | None = None
    lines: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    row = 0
    for group in paragraphs:
        first = len(lines)
        for text in group:
            layer = Image.new("L", size, 0)  # one full-size layer at a time: 17 MB each
            ImageDraw.Draw(layer).text((300, 300 + row * 100), text, font=fnt, fill=255)
            lines.append(line_truth(text, ink_box(layer)))
            ink = layer if ink is None else ImageChops.lighter(ink, layer)
            row += 1
        blocks += paragraph_blocks(lines[first:])
        row += 1  # a blank row between paragraphs
    assert ink is not None
    return Built(
        png_bytes(ImageChops.invert(ink)),
        "image",
        "px",
        "en",
        [page_truth(1, size, lines, blocks)],
        {"paper": "A3 at 300 dpi"},
    )


def build_tiff() -> Built:
    size, fnt = (1000, 300), font(TIMES, 42)
    frames, pages = [], []
    for n, text in enumerate(SMALL_PAGES, start=1):
        page, lines = render_lines(size, wrap(text, fnt, 800), fnt, x=100, y=80, leading=64)
        frames.append(page)
        pages.append(page_truth(n, size, lines, paragraph_blocks(lines)))
    data = image_bytes(frames[0], "TIFF", save_all=True, append_images=frames[1:], compression="tiff_adobe_deflate")
    return Built(data, "image", "px", "en", pages, {"frames": len(frames)})


# --- PDF fixtures -------------------------------------------------------------------------------

LETTER = (612.0, 792.0)
LATEX = r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}"
CODE = [
    "def mean(values):",
    "    total = 0.0",
    "    for value in values:",
    "        total += value",
    "    return total / len(values)",
]
HEADER = ("RAGCentre OCR verification", "Synthetic test document")
pdfmetrics.registerFont(TTFont("MathTimes", str(TIMES)))  # a TrueType subset: the formula needs √, ± and ²


def pdf_width(text: str, name: str, size: float) -> float:
    return pdfmetrics.stringWidth(text, name, size)


def pdf_wrap(text: str, name: str, size: float, width: float) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and pdf_width(trial, name, size) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return [*lines, cur]


class Doc:
    """A cursor over a reportlab canvas. ``y`` runs down from the top edge, in points.

    ``text`` records a line and its tokens (the body). Furniture, formula and code are recorded as blocks only."""

    LEFT, RIGHT, TOP, BOTTOM = 72.0, 540.0, 84.0, 716.0
    WIDTH = RIGHT - LEFT

    def __init__(self, title: str, total: int, furniture: bool = True) -> None:
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=LETTER, invariant=1, pageCompression=1)
        self.c.setTitle(title)
        self.c.setAuthor("RAGCentre OCR verification")
        self.total, self.furniture, self.pages = total, furniture, []
        self.number = 0
        self.outline = 0
        self.next_page()

    def top(self, y: float) -> float:
        return LETTER[1] - y

    def next_page(self) -> None:
        if self.number:
            self.end_page()
        self.number += 1
        self.y = self.TOP
        self.lines: list[dict[str, Any]] = []
        self.blocks: list[dict[str, Any]] = []
        if self.furniture:
            self.rule(52)
            box = [
                self.small(self.LEFT, 46, HEADER[0], "left"),
                self.small(self.RIGHT, 46, HEADER[1], "right"),
            ]
            self.blocks.append(block_truth("page_header", " ".join(HEADER), union(box)))

    def small(self, x: float, baseline: float, text: str, align: str) -> list[float]:
        """8 pt grey furniture text; it is not part of the body tokens."""
        width = pdf_width(text, "Helvetica", 8)
        x0 = x if align == "left" else x - width
        self.c.setFillGray(0.4)
        self.c.setFont("Helvetica", 8)
        self.c.drawString(x0, self.top(baseline), text)
        self.c.setFillGray(0)
        return [x0, baseline - 6, x0 + width, baseline + 2]

    def rule(self, y: float) -> None:
        self.c.setStrokeGray(0.6)
        self.c.setLineWidth(0.5)
        self.c.line(self.LEFT, self.top(y), self.RIGHT, self.top(y))
        self.c.setStrokeGray(0)

    def end_page(self) -> None:
        if self.y > self.BOTTOM:
            raise AssertionError(f"page {self.number} overflows: content ends at {self.y:.0f} pt")
        if self.furniture:
            self.rule(744)
            left = ("Confidential draft", f"Page {self.number} of {self.total}")
            box = [self.small(self.LEFT, 758, left[0], "left"), self.small(self.RIGHT, 758, left[1], "right")]
            self.blocks.append(block_truth("page_footer", " ".join(left), union(box)))
        self.c.showPage()
        self.pages.append(page_truth(self.number, LETTER, self.lines, self.blocks))

    # -- body text --
    def line(self, x: float, baseline: float, text: str, name: str, size: float) -> list[float]:
        self.c.setFillGray(0)  # a table or code box leaves its fill colour behind
        self.c.setFont(name, size)
        self.c.drawString(x, self.top(baseline), text)
        box = [x, baseline - 0.72 * size, x + pdf_width(text, name, size), baseline + 0.22 * size]
        self.lines.append(line_truth(text, box))
        return box

    def block(self, kind: str, text: str, name: str, size: float, leading: float, *, x: float = 0, width: float = 0,
              level: int | None = None, before: float = 0, after: float = 8) -> list[float]:  # fmt: skip
        x = x or self.LEFT
        self.y += before
        boxes = []
        for text_line in pdf_wrap(text, name, size, width or self.WIDTH):
            boxes.append(self.line(x, self.y + 0.8 * size, text_line, name, size))
            self.y += leading
        box = union(boxes)
        self.blocks.append(block_truth(kind, text, box, level))
        self.y += after - (leading - size)
        return box

    def paragraph(self, text: str) -> None:
        self.block("paragraph", text, "Times-Roman", 11, 15)

    def caption(self, text: str) -> None:
        self.block("caption", text, "Times-Italic", 10, 13, after=10)

    def heading(self, text: str, level: int) -> None:
        size, before = {1: (22, 0), 2: (16, 20), 3: (13, 14), 4: (11.5, 10)}[level]
        self.block("heading", text, "Helvetica-Bold", size, size * 1.3, level=level, before=before, after=6)
        if level > 1:  # the title is not in the outline
            key = f"h{self.outline}"
            self.outline += 1
            self.c.bookmarkPage(key, fit="XYZ", top=self.top(self.y - size * 1.5))
            self.c.addOutlineEntry(text, key, level=level - 2, closed=0)

    def bullets(self, items: list[str], size: float = 11, leading: float = 15) -> None:
        for item in items:
            first = self.y + 0.8 * size
            self.c.setFont("Times-Roman", size)
            self.c.drawString(self.LEFT + 8, self.top(first), "•")
            boxes = []
            for text_line in pdf_wrap(item, "Times-Roman", size, self.WIDTH - 28):
                boxes.append(self.line(self.LEFT + 28, self.y + 0.8 * size, text_line, "Times-Roman", size))
                self.y += leading
            self.blocks.append(
                block_truth("list_item", item, union([*boxes, [self.LEFT + 8, first - 8, self.LEFT + 14, first]]))
            )
        self.y += 4

    def table(self, rows: list[list[str]], col_w: list[float], row_h: float = 22) -> None:
        top, c = self.y, self.c
        width = sum(col_w)
        c.setFillGray(0.92)
        c.rect(self.LEFT, self.top(top + row_h), width, row_h, stroke=0, fill=1)
        c.setStrokeGray(0)
        c.setLineWidth(0.8)
        for r in range(len(rows) + 1):
            c.line(self.LEFT, self.top(top + r * row_h), self.LEFT + width, self.top(top + r * row_h))
        for i in range(len(col_w) + 1):
            x = self.LEFT + sum(col_w[:i])
            c.line(x, self.top(top), x, self.top(top + len(rows) * row_h))
        for r, row in enumerate(rows):
            for i, cell in enumerate(row):
                name = "Helvetica-Bold" if r == 0 else "Times-Roman"
                self.line(self.LEFT + sum(col_w[:i]) + 7, top + r * row_h + 15, cell, name, 10.5)
        box = [self.LEFT, top, self.LEFT + width, top + len(rows) * row_h]
        self.blocks.append(block_truth("table", "\n".join(" | ".join(r) for r in rows), box, html=True))
        self.y = box[3] + 12

    def formula(self) -> None:
        """A display equation as real text: the glyphs, a fraction bar and an equation number."""
        c, top, size = self.c, self.y, 13.5
        numerator, denominator, lhs = "−b ± √(b² − 4ac)", "2a", "x ="
        w_num, w_den, w_lhs = (pdf_width(t, "MathTimes", size) for t in (numerator, denominator, lhs))
        x0 = (self.LEFT + self.RIGHT) / 2 - (w_lhs + 8 + w_num) / 2
        bar_y = top + 21
        frac_x = x0 + w_lhs + 8
        c.setFont("MathTimes", size)
        c.drawString(x0, self.top(bar_y + 4), lhs)
        c.drawString(frac_x, self.top(bar_y - 5), numerator)
        c.drawString(frac_x + (w_num - w_den) / 2, self.top(bar_y + 15), denominator)
        c.setLineWidth(0.7)
        c.line(frac_x, self.top(bar_y), frac_x + w_num, self.top(bar_y))
        c.drawRightString(self.RIGHT, self.top(bar_y + 4), "(1)")
        box = [x0, top + 2, self.RIGHT, top + 40]
        self.blocks.append(block_truth("formula", LATEX, box))
        self.y = top + 40 + 14

    def code(self, lines: list[str], size: float = 9.5, pad: float = 7) -> None:
        top, lead = self.y, 12.0
        height = len(lines) * lead + 2 * pad
        self.c.setFillGray(0.95)
        self.c.setStrokeGray(0.75)
        self.c.setLineWidth(0.5)
        self.c.rect(self.LEFT, self.top(top + height), self.WIDTH, height, stroke=1, fill=1)
        self.c.setFillGray(0)
        self.c.setFont("Courier", size)
        for i, text_line in enumerate(lines):
            self.c.drawString(self.LEFT + pad, self.top(top + pad + 9 + i * lead), text_line)
        self.blocks.append(block_truth("code", "\n".join(lines), [self.LEFT, top, self.RIGHT, top + height]))
        self.y = top + height + 12

    def picture(self, png: bytes, width: float, height: float, x: float | None = None) -> list[float]:
        top = self.y
        x = self.LEFT + (self.WIDTH - width) / 2 if x is None else x
        self.c.drawImage(ImageReader(io.BytesIO(png)), x, self.top(top + height), width, height)
        box = [x, top, x + width, top + height]
        self.blocks.append(block_truth("figure", "", box))
        self.y = top + height + 8
        return box

    def finish(self) -> bytes:
        self.end_page()
        self.c.save()
        return self.buf.getvalue()


def bar_chart(values: Sequence[int], labels: Sequence[str] | None = None, title: str | None = None) -> bytes:
    """A bar chart. With ``labels`` and ``title`` its text is part of the picture (pixels), not a text layer."""
    size = (1200, 780)
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    left, bottom, top, right = 150, 640, 170 if title else 80, 1120
    for i in range(5):  # grid
        y = bottom - i * (bottom - top) // 4
        d.line([(left, y), (right, y)], fill=(210, 210, 210), width=3)
    d.line([(left, top), (left, bottom), (right, bottom)], fill="black", width=6)
    slot = (right - left) // len(values)
    biggest = max(values)
    palette = [(52, 101, 164), (204, 102, 51), (60, 140, 90), (140, 80, 150), (180, 150, 40)]
    label_font, title_font = font(ARIAL, 36), font(ARIAL_BOLD, 46)
    for i, v in enumerate(values):
        x0 = left + i * slot + slot // 6
        y0 = bottom - (bottom - top - 30) * v // biggest
        d.rectangle([x0, y0, x0 + slot * 2 // 3, bottom - 3], fill=palette[i % len(palette)])
        if labels:
            d.text((x0 + slot // 3, y0 - 12), str(v), font=label_font, fill="black", anchor="ms")
            d.text((x0 + slot // 3, bottom + 20), labels[i], font=label_font, fill="black", anchor="mt")
    if title:
        d.text((left, 70), title, font=title_font, fill="black", anchor="ls")
    return png_bytes(img)


def sign_image() -> tuple[bytes, list[str]]:
    """A photographed notice: bold capitals in a frame."""
    size, fnt = (1100, 400), font(ARIAL_BOLD, 56)
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    d.rectangle([12, 12, size[0] - 12, size[1] - 12], outline=0, width=8)
    for i, text in enumerate(SIGN):
        d.text((size[0] // 2, 150 + i * 100), text, font=fnt, fill=0, anchor="mm")
    return png_bytes(img), SIGN


def build_report() -> Built:
    doc = Doc("OCR verification report", total=5)
    doc.heading("Harbour Warehouse Audit", 1)
    doc.paragraph(
        "This report summarises the yearly audit of the harbour warehouse. It was compiled from site visits, "
        "counted inventory, and interviews with the loading crew."
    )
    doc.heading("1 Introduction", 2)
    doc.paragraph(
        "The warehouse stores ceramic tiles, timber boards, and sealed paint drums for three regional builders. "
        "Goods arrive by truck in the early morning and leave by sea before the evening tide. The audit checks "
        "whether every pallet can be found, whether the fire doors close, and whether the records match the shelves."
    )
    doc.heading("1.1 Scope", 3)
    doc.paragraph(
        "Four areas were inspected: the loading docks, the eastern wing, the cold storage room, and the open yard. "
        "Offices and the staff canteen were left out because they hold no stock. The main goals of the audit were:"
    )
    doc.bullets(
        [
            "Count every pallet and compare the total with the ledger",
            "Test each fire door and emergency light",
            "Photograph every damaged crate",
            "Interview the night shift supervisor",
        ]
    )
    doc.next_page()
    doc.heading("2 Method", 2)
    doc.paragraph(
        "Two inspectors worked in turns from Monday to Wednesday. Each pallet received a numbered tag, and each "
        "tag was scanned into a handheld ledger before photographs were taken."
    )
    doc.heading("2.1 Inventory counts", 3)
    doc.paragraph("Table 1 lists the pallets counted in each area together with the change against last year.")
    doc.caption("Table 1: Pallets counted by area")
    doc.table(TABLE, [150, 150, 150])
    doc.heading("2.1.1 Sampling rule", 4)
    doc.paragraph(
        "Every tenth pallet was opened and its contents compared with the label. Any mismatch triggered a full "
        "recount of the shelf."
    )
    doc.next_page()
    doc.heading("3 Analysis", 2)
    doc.paragraph("The crew estimated the break-even price of a pallet with the quadratic formula shown in Equation 1.")
    doc.formula()
    doc.paragraph("Here a, b, and c are the coefficients of the cost curve fitted to last year's invoices.")
    doc.heading("3.1 Implementation", 3)
    doc.paragraph(
        "The ledger totals were computed with a short script. The function below returns the mean pallet count "
        "per shelf."
    )
    doc.code(CODE)
    doc.paragraph("The script was run twice to confirm that the totals did not change.")
    doc.next_page()
    doc.heading("4 Results", 2)
    doc.paragraph(
        "Figure 1 shows how the daily count changed during the audit week. Counts rose steadily on Monday and "
        "Tuesday and settled on Wednesday."
    )
    doc.picture(bar_chart([62, 88, 120, 118, 121]), 360, 234)
    doc.caption("Figure 1: Daily pallet count during the audit week.")
    doc.paragraph(
        "No pallet was lost. Eleven labels were torn, three crates were cracked, and one fire door stuck in its frame."
    )
    doc.next_page()
    doc.heading("5 Conclusion", 2)
    doc.paragraph("The warehouse is in good order. The recommended actions are listed below.")
    doc.bullets(
        [
            "Replace the sticking fire door in the eastern wing",
            "Print new labels for the eleven torn pallets",
            "Repair the three cracked crates before shipping",
            "Repeat the count in the cold storage room next spring",
        ]
    )
    doc.paragraph("The next audit is planned for the same week next year.")
    data = doc.finish()
    return Built(
        data, "pdf", "pt", "en", doc.pages,
        {
            "text_layer": True,
            "heading_levels": {"title": 1, "section": 2, "subsection": 3, "subsubsection": 4},
            "table": {"page": 2, "rows": 4, "cols": 3, "cells": TABLE, "ruled": True},
            "formula": {"page": 3, "latex": LATEX, "numbered": "(1)", "in_tokens": False,
                        "made_as": "vector text glyphs (Times New Roman subset) laid out by hand: numerator, fraction bar, denominator"},
            "code": {"page": 3, "language": "python", "text": "\n".join(CODE), "in_tokens": False},
            "figure": {"page": 4, "made_as": "raster PNG bar chart with no text in it", "caption": "Figure 1: Daily pallet count during the audit week."},
            "furniture": {"header": list(HEADER), "footer": ["Confidential draft", "Page N of 5"], "on_every_page": True},
            "outline": "bookmarks for every section heading, levels 0 to 2",
            "bbox_precision": "line boxes come from font metrics, good to about 2 pt",
        },
    )  # fmt: skip


def posterize(img: Image.Image, levels: int = 16) -> Image.Image:
    step = 255 / (levels - 1)
    return img.point(lambda v: round(round(v / step) * step))


def build_scan_pdf() -> Built:
    """Five image-only pages: no text layer. Each page is a 200 dpi greyscale image of 14 pt text."""
    px, scale = (1700, 2200), 612 / 1700
    fnt = font(TIMES, 39)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER, invariant=1, pageCompression=1)
    c.setTitle("Scanned pages")
    pages = []
    for n, text in enumerate(MULTIPAGE, start=1):
        img, lines = render_lines(px, wrap(text, fnt, px[0] - 440), fnt, x=220, y=260, leading=62)
        c.drawImage(ImageReader(posterize(img)), 0, 0, LETTER[0], LETTER[1])
        c.showPage()
        lines = [line_truth(ln["text"], [v * scale for v in ln["bbox"]]) for ln in lines]
        pages.append(page_truth(n, LETTER, lines, paragraph_blocks(lines)))
    c.save()
    return Built(buf.getvalue(), "pdf", "pt", "en", pages, {"text_layer": False, "source_dpi": 200})


def build_mixed_pdf() -> Built:
    """Born-digital text and a small picture of text. The picture is 3% of the page, under the 5% bitmap area
    that Docling OCRs by default, so only a full-page OCR reads it."""
    doc = Doc("Field notes", total=1, furniture=False)
    doc.heading("Field Notes", 2)
    doc.paragraph(
        "The eastern quay was quiet on Thursday morning. A notice hung on the fence, and it is reproduced below "
        "as a photograph for the file."
    )
    sign, sign_tokens = sign_image()
    box = doc.picture(sign, 200, 200 * 400 / 1100, x=Doc.LEFT)
    doc.paragraph("The crew heard about the change by radio before the first shift began.")
    page = doc.finish()
    return Built(
        page, "pdf", "pt", "en", doc.pages,
        {
            "text_layer": True,
            "image_tokens": [t for s in sign_tokens for t in tokenize(s)],
            "image_text": sign_tokens,
            "image_bbox": [round(v, 2) for v in box],
            "image_area_fraction": round((box[2] - box[0]) * (box[3] - box[1]) / (LETTER[0] * LETTER[1]), 4),
        },
    )  # fmt: skip


def build_figure_pdf() -> Built:
    """Born-digital text around a chart whose labels, title and values exist only as pixels."""
    doc = Doc("Weekly summary", total=1, furniture=False)
    doc.heading("Weekly Summary", 2)
    doc.paragraph("The chart below compares the storage zones after the recount.")
    box = doc.picture(bar_chart(CHART_VALUES, CHART_LABELS, CHART_TITLE), 400, 260)
    doc.caption("Figure 1: Recount results for the four storage zones.")
    page = doc.finish()
    figure_tokens = tokenize(" ".join([CHART_TITLE, *CHART_LABELS, *map(str, CHART_VALUES)]))
    return Built(
        page, "pdf", "pt", "en", doc.pages,
        {
            "text_layer": True,
            "figure_tokens": figure_tokens,
            "figure_text": [CHART_TITLE, *CHART_LABELS, *map(str, CHART_VALUES)],
            "figure_bbox": [round(v, 2) for v in box],
            "figure_area_fraction": round((box[2] - box[0]) * (box[3] - box[1]) / (LETTER[0] * LETTER[1]), 4),
        },
    )  # fmt: skip


# --- office and text files ----------------------------------------------------------------------

NOTE = [
    "The committee met on Monday to review the warehouse audit.",
    "Two actions were agreed and an owner was named for each.",
]
ACTIONS = ["Replace the sticking fire door", "Print new pallet labels", "Repair three cracked crates"]
OWNERS = [["Item", "Owner"], ["Fire door", "Maria"], ["Labels", "Jonas"]]


def office_truth(blocks: list[dict[str, Any]], texts: list[str]) -> list[dict[str, Any]]:
    """One page without geometry: width and height 0, no boxes."""
    lines = [line_truth(t, [0, 0, 0, 0]) for t in texts]
    return [page_truth(1, (0, 0), lines, blocks)]


def build_docx() -> Built:
    def para(text: str, style: str | None = None) -> str:
        pr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f"<w:p>{pr}<w:r><w:t>{text}</w:t></w:r></w:p>"

    def cell(text: str) -> str:
        return f'<w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr>{para(text)}</w:tc>'

    rows = "".join(f"<w:tr>{''.join(cell(t) for t in row)}</w:tr>" for row in OWNERS)
    table = (
        "<w:tbl><w:tblPr><w:tblBorders>"
        + "".join(
            f'<w:{e} w:val="single" w:sz="4" w:color="000000"/>'
            for e in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        + '</w:tblBorders></w:tblPr><w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid>'
        + rows
        + "</w:tbl>"
    )
    body = (
        para("Meeting Notes", "Heading1")
        + "".join(para(t) for t in NOTE)
        + "".join(para(t, "ListBullet") for t in ACTIONS)
        + table
    )
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {ns}><w:body>{body}</w:body></w:document>'
    )

    def style(sid: str, name: str, extra: str = "") -> str:
        return f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/><w:basedOn w:val="Normal"/>{extra}</w:style>'

    styles = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {ns}>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        + style(
            "Heading1", "heading 1", '<w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="36"/></w:rPr>'
        )
        + style("ListBullet", "List Bullet", '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>')
        + "</w:styles>"
    )
    types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel}/styles" Target="styles.xml"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in [
            ("[Content_Types].xml", types),
            ("_rels/.rels", rels),
            ("word/document.xml", document),
            ("word/styles.xml", styles),
            ("word/_rels/document.xml.rels", doc_rels),
        ]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))  # a fixed date keeps the bytes identical
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, content)
    cells = [c for row in OWNERS for c in row]
    blocks = [
        block_truth("heading", "Meeting Notes", [0, 0, 0, 0], 1),
        *(block_truth("paragraph", t, [0, 0, 0, 0]) for t in NOTE),
        *(block_truth("list_item", t, [0, 0, 0, 0]) for t in ACTIONS),
        block_truth("table", "\n".join(" | ".join(r) for r in OWNERS), [0, 0, 0, 0], html=True),
    ]
    texts = ["Meeting Notes", *NOTE, *ACTIONS, *cells]
    return Built(
        buf.getvalue(),
        "office",
        "none",
        "en",
        office_truth(blocks, texts),
        {"table": {"rows": 3, "cols": 2, "cells": OWNERS}},
    )


def text_fixture(kind: str, text: str, blocks: list[dict[str, Any]], texts: list[str], **extras: Any) -> Built:
    return Built(text.encode("utf-8"), kind, "none", "en", office_truth(blocks, texts), extras)


def build_text_files() -> dict[str, Built]:
    zero = [0, 0, 0, 0]
    schedule = ["Dock Schedule", "Trucks arrive before dawn and leave before noon.", "Arrivals", *ACTIONS]
    html = (
        '<!DOCTYPE html>\n<html lang="en">\n<head><meta charset="utf-8"><title>Dock Schedule</title></head>\n<body>\n'
        f"<h1>{schedule[0]}</h1>\n<p>{schedule[1]}</p>\n<h2>{schedule[2]}</h2>\n<ul>\n"
        + "".join(f"<li>{t}</li>\n" for t in ACTIONS)
        + '</ul>\n<table border="1">\n'
        + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>\n" for row in TABLE)
        + "</table>\n</body>\n</html>\n"
    )
    html_blocks = [
        block_truth("heading", schedule[0], zero, 1),
        block_truth("paragraph", schedule[1], zero),
        block_truth("heading", schedule[2], zero, 2),
        *(block_truth("list_item", t, zero) for t in ACTIONS),
        block_truth("table", "\n".join(" | ".join(r) for r in TABLE), zero, html=True),
    ]
    cells = [c for row in TABLE for c in row]
    csv = "".join(",".join(row) + "\n" for row in TABLE)
    note_txt = "\n\n".join(NOTE) + "\n"
    note_md = "# Meeting Notes\n\n" + "\n\n".join(NOTE) + "\n\n" + "".join(f"- {t}\n" for t in ACTIONS)
    return {
        "sample.html": text_fixture("html", html, html_blocks, [*schedule, *cells], table={"rows": 4, "cols": 3, "cells": TABLE}),
        "sample.csv": text_fixture(
            "csv", csv, [block_truth("table", "\n".join(" | ".join(r) for r in TABLE), zero, html=True)], cells,
            table={"rows": 4, "cols": 3, "cells": TABLE},
        ),
        "note.txt": text_fixture("text", note_txt, [block_truth("paragraph", t, zero) for t in NOTE], NOTE),
        "note.md": text_fixture(
            "text", note_md,
            [block_truth("heading", "Meeting Notes", zero, 1), *(block_truth("paragraph", t, zero) for t in NOTE), *(block_truth("list_item", t, zero) for t in ACTIONS)],
            ["Meeting Notes", *NOTE, *ACTIONS],
        ),
    }  # fmt: skip


# --- checks -------------------------------------------------------------------------------------


def check_ink(img: Image.Image, boxes: Sequence[Sequence[float]], what: str, pad: int = 4) -> None:
    """Every box holds ink, and no dark pixel lies outside every box."""
    dark = img.convert("L").point(lambda v: 255 if v < 128 else 0)
    mask = Image.new("L", img.size, 0)
    for b in boxes:
        ImageDraw.Draw(mask).rectangle([b[0] - pad, b[1] - pad, b[2] + pad - 1, b[3] + pad - 1], fill=255)
        if dark.crop(tuple(int(v) for v in b)).getbbox() is None:
            raise AssertionError(f"{what}: line box {list(b)} holds no ink")
    stray = ImageChops.subtract(dark, mask).getbbox()
    if stray:
        raise AssertionError(f"{what}: ink outside every line box, near {stray}")


def verify(name: str, built: Built) -> None:
    if len(built.data) >= MAX_BYTES:
        raise AssertionError(f"{name}: {len(built.data)} bytes, the limit is {MAX_BYTES}")
    unchecked = {"scan_noisy.png", "scan_table.png"}  # blurred or ruled: ink is not only the line boxes
    if built.kind == "image" and name not in unchecked and not name.endswith(".gif"):
        with Image.open(io.BytesIO(built.data)) as img:
            for n, page in enumerate(built.pages):
                img.seek(n)
                check_ink(img.copy(), [ln["bbox"] for ln in page["lines"]], f"{name} page {n + 1}")
    for page in built.pages:
        if not page["tokens"] and name != "report_born_digital.pdf" and built.kind == "image":
            raise AssertionError(f"{name}: page {page['page']} has no tokens")
    # Text that only a full-page OCR or a figure reader can find must not also sit in the text layer.
    for key in ("image_tokens", "figure_tokens"):
        if key in built.extras:
            layer = [t for page in built.pages for t in page["tokens"]]
            if recall(built.extras[key], layer) > 0.15:
                raise AssertionError(f"{name}: {key} are readable from the text layer")


# --- build and write ----------------------------------------------------------------------------


def build_all() -> dict[str, Built]:
    rng = random.Random(SEED)
    built = build_scans(rng)
    built["scan_table.png"] = build_table_image()
    built["large_page.png"] = build_large_page()
    built["multi_frame.tiff"] = build_tiff()
    built["report_born_digital.pdf"] = build_report()
    built["scan_multipage.pdf"] = build_scan_pdf()
    built["mixed_embedded_image.pdf"] = build_mixed_pdf()
    built["figure_with_text.pdf"] = build_figure_pdf()
    built["sample.docx"] = build_docx()
    built.update(build_text_files())
    return dict(sorted(built.items()))


def write(out: Path, built: dict[str, Built]) -> None:
    (out / "fixtures").mkdir(parents=True, exist_ok=True)
    fixtures: dict[str, Any] = {}
    for name, b in built.items():
        verify(name, b)
        (out / "fixtures" / name).write_bytes(b.data)
        fixtures[name] = {
            "kind": b.kind,
            "units": b.units,
            "language": b.language,
            "sha256": hashlib.sha256(b.data).hexdigest(),
            "bytes": len(b.data),
            "pages": b.pages,
            "extras": b.extras,
        }
    truth = {"seed": SEED, "fixtures": fixtures}
    (out / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=HERE, help="folder that gets fixtures/ and truth.json")
    args = parser.parse_args(argv)
    missing = [str(p) for p in (TIMES, ARIAL, ARIAL_BOLD, COURIER, CHINESE) if not p.is_file()]
    if missing:
        print("the fixtures are drawn with macOS system fonts, which are missing here: " + ", ".join(missing))
        return EXIT_NO_FONTS
    built = build_all()
    write(args.out, built)
    for name, b in built.items():
        print(f"{name:32} {len(b.data):>8} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
