"""Live check: parse a small generated PDF through the real engine.

Skipped unless ``SHIM_LIVE=1`` and MinerU can be imported. It checks only that the result is a
well-formed Document IR with at least one block that has a page and a bbox, not what the engine read.
Run it in an environment that has the engine and its models (the first run downloads them)::

    SHIM_LIVE=1 uv run pytest tests/test_live.py -s
"""

from __future__ import annotations

import importlib.util
import os

import pytest
from fastapi.testclient import TestClient

from mineru_shim.app import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("SHIM_LIVE") != "1" or importlib.util.find_spec("mineru") is None,
    reason="set SHIM_LIVE=1 in an environment with MinerU installed",
)

BLOCK_TYPES = {
    "heading", "paragraph", "list_item", "table", "figure", "code", "formula", "caption", "footnote",
    "page_header", "page_footer",
}  # fmt: skip
OPTIONS = {"tier": "flash"}


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
    """Every parameter of ``mineru.parse`` and every field of ``VlmConfig`` is reached by an option or a
    service setting, or left out on purpose. A MinerU upgrade that adds one fails this."""
    import inspect

    import mineru
    from mineru.config import VlmConfig

    from mineru_shim.engine import NAMED_PARAMETERS, NOT_EXPOSED

    for label, actual in (
        ("parse", set(inspect.signature(mineru.parse).parameters)),
        ("VlmConfig", set(VlmConfig.model_fields)),
    ):
        known = set(NAMED_PARAMETERS[label]) | set(NOT_EXPOSED[label])
        assert actual - known == set(), f"new {label} parameters: {sorted(actual - known)}"
        assert known - actual == set(), f"{label} parameters MinerU no longer has: {sorted(known - actual)}"
