"""Live check: read a small generated PDF through the real engine.

Skipped unless ``SHIM_LIVE=1`` and EasyOCR can be imported. It renders a one-page PDF with a title and
two sentences, and checks that the result is a well-formed Document IR in which the engine read the
words, with a page and a bbox on every block. Run it in an environment that has the engine (the
first run downloads its models)::

    SHIM_LIVE=1 uv run pytest tests/test_live.py -s
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest
from fastapi.testclient import TestClient

from easyocr_shim.app import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("SHIM_LIVE") != "1" or importlib.util.find_spec("easyocr") is None,
    reason="set SHIM_LIVE=1 in an environment with EasyOCR installed",
)


def make_pdf() -> bytes:
    """A one-page PDF with a large title and two sentences, assembled by hand (no PDF library needed)."""
    content = (
        b"BT /F1 32 Tf 72 690 Td (Pump Manual) Tj ET\n"
        b"BT /F1 14 Tf 72 640 Td (Mount the pump on a level surface.) Tj ET\n"
        b"BT /F1 14 Tf 72 560 Td (Close the valve before servicing.) Tj ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_live_parse() -> None:
    client = TestClient(create_app())
    info = client.get("/info").json()
    print("service version:", info["version"])
    resp = client.post(
        "/parse",
        files={"file": ("pump.pdf", make_pdf(), "application/pdf")},
        data={"options": json.dumps({"headings": True})},
    )
    assert resp.status_code == 200, resp.text
    ir = resp.json()
    print(json.dumps(ir, indent=1)[:2000])
    assert ir["schema_version"] == "1.0" and ir["metadata"]["page_count"] == 1
    blocks = ir["blocks"]
    assert blocks, "the engine found no text"
    assert len({b["id"] for b in blocks}) == len(blocks)
    text = " ".join(b["text"] for b in blocks).lower()
    for expected in ("pump", "manual", "level", "valve"):
        assert expected in text, f"{expected!r} was not read: {text!r}"
    page = ir["pages"][0]
    for block in blocks:
        assert block["page"] == 1
        x0, y0, x1, y1 = block["bbox"]
        assert 0 <= x0 < x1 <= page["width"] and 0 <= y0 < y1 <= page["height"]
        assert 0 <= block["metadata"]["confidence"] <= 1
    # The 32-point title is far taller than the 14-point text: the heading guess finds it.
    assert blocks[0]["type"] == "heading" and blocks[0]["level"] == 1, blocks[0]
