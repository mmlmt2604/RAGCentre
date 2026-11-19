"""A small two-page PDF for checking a parser service (``ragcentre parser check``).

It is built by hand, so nothing is installed or shipped: a "Pump Manual" with a title, two headings
of different sizes, a paragraph, a bullet list, a ruled two-row table, a figure caption, and a
header and footer repeated on both pages. ``EXPECTED_ORDER`` lists texts in reading order.
"""

from __future__ import annotations

HEADER = "Pump Manual - Confidential"

# (page, font size, x, y from the bottom, text); a 612 x 792 point page.
_TEXT: list[tuple[int, int, int, int, str]] = [
    (1, 9, 72, 760, HEADER),
    (1, 26, 72, 700, "Pump Manual"),
    (1, 16, 72, 640, "Installation"),
    (1, 11, 72, 610, "Mount the pump on a level surface and secure it with four bolts."),
    (1, 11, 90, 580, "- Close the inlet valve."),
    (1, 11, 90, 562, "- Connect the outlet hose."),
    (1, 9, 72, 40, "Page 1"),
    (2, 9, 72, 760, HEADER),
    (2, 16, 72, 700, "Specifications"),
    (2, 11, 72, 670, "The table lists the rated values of the pump."),
    (2, 11, 80, 624, "Flow"),
    (2, 11, 280, 624, "40 l/min"),
    (2, 11, 80, 594, "Pressure"),
    (2, 11, 280, 594, "4 bar"),
    (2, 10, 72, 440, "Figure 1: exploded view"),
    (2, 9, 72, 40, "Page 2"),
]
# (page, x, y, width, height) rectangles that draw the table grid
_TABLE = [(2, 72, 580, 200, 30), (2, 272, 580, 200, 30), (2, 72, 610, 200, 30), (2, 272, 610, 200, 30)]
# A box standing in for the figure the caption describes.
_FIGURE = (2, 72, 450, 200, 100)

# What a parser should read, in reading order, ignoring the header and footer.
EXPECTED_ORDER = [
    "Pump Manual",
    "Installation",
    "Mount the pump",
    "Close the inlet valve",
    "Connect the outlet hose",
    "Specifications",
    "40 l/min",
    "4 bar",
    "Figure 1",
]
EXPECTED_HEADINGS = ["Pump Manual", "Installation", "Specifications"]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _page_stream(page: int) -> bytes:
    parts: list[str] = []
    for number, x, y, w, h in [*_TABLE, _FIGURE]:
        if number == page:
            parts.append(f"0.8 w {x} {y} {w} {h} re S")
    for number, size, x, y, text in _TEXT:
        if number == page:
            font = "F2" if size >= 16 else "F1"
            parts.append(f"BT /{font} {size} Tf {x} {y} Td ({_escape(text)}) Tj ET")
    return "\n".join(parts).encode("latin-1")


def sample_pdf() -> bytes:
    """The sample document as PDF bytes (Helvetica, no embedded fonts, a text layer)."""
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")  # 1
    objects.append(b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>")  # 2
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R "
        b"/Resources << /Font << /F1 7 0 R /F2 8 0 R >> >> >>"
    )
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 6 0 R "
        b"/Resources << /Font << /F1 7 0 R /F2 8 0 R >> >> >>"
    )
    for page in (1, 2):
        stream = _page_stream(page)
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    # 3, 4: pages; 5, 6: their content streams; 7, 8: fonts.

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)
