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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont  # noqa: E402
from predicates import tokenize  # noqa: E402  the tokenizer recall uses, so truth and results agree

SEED = 20261128
MAX_BYTES = 200 * 1024

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
    add("scan_clean.gif", page, image_bytes(page, "GIF"), lines, note="a GIF: every engine must refuse it with 415")
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
        if not page["tokens"] and built.kind != "office":
            raise AssertionError(f"{name}: page {page['page']} has no tokens")


# --- build and write ----------------------------------------------------------------------------


def build_all() -> dict[str, Built]:
    rng = random.Random(SEED)
    built = build_scans(rng)
    built["scan_table.png"] = build_table_image()
    built["large_page.png"] = build_large_page()
    built["multi_frame.tiff"] = build_tiff()
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
    built = build_all()
    write(args.out, built)
    for name, b in built.items():
        print(f"{name:32} {len(b.data):>8} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
