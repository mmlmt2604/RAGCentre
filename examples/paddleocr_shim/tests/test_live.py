"""Live check: parse a small generated PDF through the real engine.

Skipped unless ``SHIM_LIVE=1`` and PaddleOCR can be imported. It checks only that the result is a
well-formed Document IR with at least one block that has a page and a bbox, not what the engine read.
Run it in an environment that has the engine and its models (the first run downloads them)::

    SHIM_LIVE=1 uv run pytest tests/test_live.py -s
"""

from __future__ import annotations

import importlib.util
import os

import pytest
from fastapi.testclient import TestClient

from paddleocr_shim.app import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("SHIM_LIVE") != "1" or importlib.util.find_spec("paddleocr") is None,
    reason="set SHIM_LIVE=1 in an environment with PaddleOCR installed",
)

BLOCK_TYPES = {
    "heading", "paragraph", "list_item", "table", "figure", "code", "formula", "caption", "footnote",
    "page_header", "page_footer",
}  # fmt: skip
OPTIONS = {}


def make_pdf() -> bytes:
    """A one-page PDF with a heading and a sentence, assembled by hand (no PDF library needed)."""
    content = (
        b"BT /F1 24 Tf 72 700 Td (Pump Manual) Tj ET\n"
        b"BT /F1 12 Tf 72 660 Td (Mount the pump on a level surface.) Tj ET\n"
        b"BT /F1 12 Tf 72 640 Td (Close the valve before servicing.) Tj ET"
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
    import json

    client = TestClient(create_app())
    info = client.get("/info").json()
    print("service version:", info["version"])
    resp = client.post(
        "/parse",
        files={"file": ("pump.pdf", make_pdf(), "application/pdf")},
        data={"options": json.dumps(OPTIONS)},
    )
    assert resp.status_code == 200, resp.text
    ir = resp.json()
    print(json.dumps(ir, indent=1)[:2000])
    assert ir["schema_version"] == "1.1"
    blocks = ir["blocks"]
    assert blocks, "the engine found no text"
    assert len({b["id"] for b in blocks}) == len(blocks)
    for block in blocks:
        assert block["type"] in BLOCK_TYPES or isinstance(block["type"], str)
        assert isinstance(block["text"], str)
        if block["type"] == "heading":
            assert 1 <= block["level"] <= 6
    located = [b for b in blocks if isinstance(b.get("page"), int) and b["page"] >= 1 and len(b.get("bbox", [])) == 4]
    assert located, "no block has a page and a bbox"
    for b in located:
        x0, y0, x1, y1 = b["bbox"]
        assert x0 <= x1 and y0 <= y1


def test_engine_parameters_accounted_for() -> None:
    """Every argument of PP-StructureV3's constructor and ``predict`` is reached by a named option,
    allowed through ``engine``, or refused on purpose. A PaddleOCR upgrade that adds one fails this."""
    import inspect

    from paddleocr import PPStructureV3
    from shimkit import DENIED_ENGINE_KEYS
    from shimkit.service import DENIED_ENGINE_SUFFIXES

    from paddleocr_shim.engine import (
        ENGINE_INIT_ARGUMENTS,
        ENGINE_PREDICT_ARGUMENTS,
        NAMED_ARGUMENTS,
        NOT_EXPOSED,
    )

    def names(function: object) -> set[str]:
        parameters = inspect.signature(function).parameters  # type: ignore[call-overload]
        return {n for n, p in parameters.items() if n != "self" and p.kind is not p.VAR_KEYWORD}

    def refused(name: str) -> bool:
        return name in DENIED_ENGINE_KEYS or name.endswith(DENIED_ENGINE_SUFFIXES)

    for label, actual, allowed in (
        ("__init__", names(PPStructureV3.__init__), ENGINE_INIT_ARGUMENTS),
        ("predict", names(PPStructureV3.predict), ENGINE_PREDICT_ARGUMENTS),
    ):
        known = set(NAMED_ARGUMENTS) | set(allowed) | set(NOT_EXPOSED) | {n for n in actual if refused(n)}
        assert actual - known == set(), f"new PPStructureV3.{label} arguments: {sorted(actual - known)}"
        assert set(allowed) - actual == set(), (
            f"{label} arguments PaddleOCR no longer has: {sorted(set(allowed) - actual)}"
        )
    assert (set(NAMED_ARGUMENTS) - {"lang", "ocr_version"}) <= names(PPStructureV3.predict)
