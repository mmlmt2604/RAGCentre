"""Live check: parse generated documents through the real engine.

Skipped unless ``SHIM_LIVE=1`` and PaddleOCR can be imported. The first run downloads the layout model;
the recognition model runs where the ``PADDLEOCR_VL_SHIM_*`` settings say (the ``native`` backend needs
its own download and a machine that can run it; the README shows a ``llama-cpp-server`` setup). With no
settings it is the native backend::

    SHIM_LIVE=1 uv run pytest tests/test_live.py -s

The parse tests check what the engine read from two documents that are easy to read (a heading, a
paragraph and a ruled table), loosely: a different model may phrase a cell differently. The drift tests
check that the shim's tables still describe the installed PaddleOCR.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from paddleocr_vl_shim.app import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("SHIM_LIVE") != "1" or importlib.util.find_spec("paddleocr") is None,
    reason="set SHIM_LIVE=1 in an environment with PaddleOCR installed",
)

ROWS = [
    ("Model", "Flow (l/min)", "Power (kW)"),
    ("P-100", "40", "1.5"),
    ("P-200", "85", "3.0"),
    ("P-300", "120", "5.5"),
]


def make_scan() -> bytes:
    """A PNG of a page with a heading, a section title, a paragraph and a ruled table."""
    import io

    from PIL import Image, ImageDraw, ImageFont

    def font(size: int, bold: bool = False) -> Any:
        for path in ("/System/Library/Fonts/Helvetica.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            if os.path.exists(path):
                return ImageFont.truetype(path, size, index=1 if bold and path.endswith(".ttc") else 0)
        return ImageFont.load_default(size)

    image = Image.new("RGB", (1240, 1754), "white")  # A4 at 150 dpi
    draw = ImageDraw.Draw(image)
    draw.text((120, 140), "Pump Maintenance Manual", font=font(64, True), fill="black")
    draw.text((120, 260), "1. Installation", font=font(44, True), fill="black")
    lines = [
        "Mount the pump on a level surface and tighten the four anchor bolts.",
        "Close the inlet valve before servicing the unit. Check the seals every",
        "six months and replace any that show wear.",
    ]
    for number, line in enumerate(lines):
        draw.text((120, 340 + number * 44), line, font=font(32), fill="black")
    left, top, width, height = 120, 560, 330, 70
    for r, row in enumerate(ROWS):
        for c, cell in enumerate(row):
            draw.text((left + c * width + 20, top + r * height + 16), cell, font=font(32, r == 0), fill="black")
    for r in range(len(ROWS) + 1):
        draw.line([(left, top + r * height), (left + 3 * width, top + r * height)], fill="black", width=3)
    for c in range(4):
        draw.line([(left + c * width, top), (left + c * width, top + len(ROWS) * height)], fill="black", width=3)
    draw.text((120, 900), "Table 1: Pump models and ratings", font=font(28), fill="black")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def make_pdf() -> bytes:
    """A one-page PDF with a text layer: a title, a section title, two sentences and a ruled table,
    assembled by hand (no PDF library needed)."""
    ops = [b"BT /F2 24 Tf 72 720 Td (Pump Maintenance Manual) Tj ET", b"BT /F2 16 Tf 72 680 Td (1. Installation) Tj ET"]
    sentences = [
        "Mount the pump on a level surface and tighten the four anchor bolts.",
        "Close the inlet valve before servicing the unit.",
    ]
    for number, sentence in enumerate(sentences):
        ops.append(b"BT /F1 12 Tf 72 %d Td (%s) Tj ET" % (650 - number * 18, sentence.encode()))
    for r, row in enumerate(ROWS):
        for c, cell in enumerate(row):
            ops.append(b"BT /F1 12 Tf %d %d Td (%s) Tj ET" % (80 + c * 150, 560 - r * 30, cell.encode()))
    for r in range(len(ROWS) + 1):
        ops.append(b"72 %d m 522 %d l S" % (570 - r * 30, 570 - r * 30))
    for c in range(4):
        ops.append(b"%d 570 m %d %d l S" % (72 + c * 150, 72 + c * 150, 570 - len(ROWS) * 30))
    content = b"\n".join(ops)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
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


@pytest.fixture(scope="module")
def client() -> Any:
    with TestClient(create_app()) as client:  # one app: the pipeline is built once for the module
        info = client.get("/info").json()
        print("service version:", info["version"])
        yield client


def parse(client: TestClient, name: str, content: bytes, options: dict[str, Any] | None = None) -> dict[str, Any]:
    resp = client.post(
        "/parse",
        files={"file": (name, content, "application/octet-stream")},
        data={"options": json.dumps(options or {})},
    )
    assert resp.status_code == 200, resp.text
    ir = resp.json()
    print(json.dumps(ir, indent=1)[:3000])
    assert ir["schema_version"] == "1.1"
    blocks = ir["blocks"]
    assert blocks, "the engine found nothing"
    assert len({b["id"] for b in blocks}) == len(blocks)
    for block in blocks:
        assert isinstance(block["type"], str) and isinstance(block["text"], str)
        if block["type"] == "heading":
            assert 1 <= block["level"] <= 6
    for block in blocks:
        assert isinstance(block.get("page"), int) and block["page"] >= 1
        x0, y0, x1, y1 = block["bbox"]
        assert x0 <= x1 and y0 <= y1
    return ir


def check_page(ir: dict[str, Any]) -> None:
    """What the page above must produce: its headings, its sentence, its table."""
    text = " ".join(b["text"] for b in ir["blocks"])
    assert "Pump Maintenance Manual" in text and "Installation" in text
    assert "level surface" in text and "anchor bolts" in text
    assert [b["type"] for b in ir["blocks"]].count("heading") >= 2
    (table,) = [b for b in ir["blocks"] if b["type"] == "table"]
    assert table["html"].startswith("<table") and "P-200" in table["html"]
    assert "85" in table["text"] and "Flow" in table["text"]
    # reading order: the title comes before the table
    order = [b["type"] for b in ir["blocks"]]
    assert order.index("heading") < order.index("table")


def test_live_scanned_page(client: TestClient) -> None:
    ir = parse(client, "scan.png", make_scan())
    check_page(ir)
    assert ir["pages"] == [{"page": 1, "width": 1240.0, "height": 1754.0}]  # a single image is page 1
    assert any(b["type"] == "caption" and "Table 1" in b["text"] for b in ir["blocks"])


def test_live_pdf_with_a_text_layer(client: TestClient) -> None:
    ir = parse(client, "pump.pdf", make_pdf())
    check_page(ir)
    assert [p["page"] for p in ir["pages"]] == [1] and {b["page"] for b in ir["blocks"]} == {1}


def test_live_options_reach_the_engine(client: TestClient) -> None:
    flat = parse(client, "scan.png", make_scan(), {"heading_levels": False, "merge_tables": False})
    assert {b["level"] for b in flat["blocks"] if b["type"] == "heading" and b["level"] > 1} <= {2}
    blank = parse(client, "scan.png", make_scan(), {"layout_detection": False})  # the whole page as one block
    assert len(blank["blocks"]) == 1 and blank["blocks"][0]["type"] == "paragraph"
    assert "Pump Maintenance Manual" in blank["blocks"][0]["text"]
    sampled = parse(client, "scan.png", make_scan(), {"engine": {"temperature": 0.0, "max_new_tokens": 1024}})
    check_page(sampled)


def test_live_title_levels_come_from_the_second_step() -> None:
    """``predict`` leaves ``title_level`` unset; ``restructure_pages`` sets it (read in the source, here run)."""
    import tempfile

    from paddleocr_vl_shim.engine import OPTION_ARGUMENTS, PaddleVLEngine

    engine = PaddleVLEngine()
    flags = {argument: False for argument in OPTION_ARGUMENTS.values()} | {"use_layout_detection": True}
    pipeline = engine._pipeline(flags)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "scan.png"
        path.write_bytes(make_scan())
        results = list(pipeline.predict(str(path), **flags))
    titles = [b for r in results for b in r["parsing_res_list"] if b.label == "paragraph_title"]
    assert titles and all(not hasattr(b, "title_level") for b in titles)
    results = list(pipeline.restructure_pages(results))
    titles = [b for r in results for b in r["parsing_res_list"] if b.label == "paragraph_title"]
    assert titles and all(isinstance(b.title_level, int) and b.title_level >= 1 for b in titles)
    assert all(r.get("page_index") is None for r in results)  # a single image
    engine.close()


def test_live_a_server_that_is_down_is_503() -> None:
    """Needs a server backend in the environment (the failure is the server's, not the model's)."""
    from paddleocr_vl_shim.engine import PaddleVLEngine, RecognitionSettings

    if os.environ.get("PADDLEOCR_VL_SHIM_REC_BACKEND", "native") == "native":
        pytest.skip("set PADDLEOCR_VL_SHIM_REC_BACKEND to a server backend")
    settings = RecognitionSettings(
        backend=os.environ["PADDLEOCR_VL_SHIM_REC_BACKEND"], server_url="http://127.0.0.1:9/v1"
    )
    engine = PaddleVLEngine(settings)
    try:
        with TestClient(create_app(engine)) as down:
            resp = down.post("/parse", files={"file": ("scan.png", make_scan(), "image/png")})
    finally:
        engine.close()
    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert error["code"] == "engine_unavailable" and "PADDLEOCR_VL_SHIM_REC_SERVER_URL" in error["message"]
    assert "127.0.0.1" not in error["message"]


# --- drift ----------------------------------------------------------------------------------------


def test_engine_parameters_accounted_for() -> None:
    """Every argument of ``PaddleOCRVL.__init__`` and ``predict`` is reached by a named option, allowed
    through ``engine``, a service setting, or refused on purpose. A PaddleOCR upgrade that adds one fails this."""
    import inspect

    from paddleocr import PaddleOCRVL
    from shimkit import DENIED_ENGINE_KEYS
    from shimkit.service import DENIED_ENGINE_SUFFIXES

    from paddleocr_vl_shim.engine import (
        ENGINE_INIT_ARGUMENTS,
        ENGINE_PREDICT_ARGUMENTS,
        NAMED_ARGUMENTS,
        NOT_EXPOSED,
        SERVICE_SETTING_ARGUMENTS,
    )

    def names(function: object) -> set[str]:
        parameters = inspect.signature(function).parameters  # type: ignore[call-overload]
        return {n for n, p in parameters.items() if n != "self" and p.kind is not p.VAR_KEYWORD}

    def refused(name: str) -> bool:
        return name in DENIED_ENGINE_KEYS or name.endswith(DENIED_ENGINE_SUFFIXES)

    init, predict = names(PaddleOCRVL.__init__), names(PaddleOCRVL.predict)
    for label, actual, allowed, settings in (
        ("__init__", init, ENGINE_INIT_ARGUMENTS, set(SERVICE_SETTING_ARGUMENTS)),
        ("predict", predict, ENGINE_PREDICT_ARGUMENTS, set()),
    ):
        known = set(NAMED_ARGUMENTS) | set(allowed) | settings | set(NOT_EXPOSED) | {n for n in actual if refused(n)}
        assert actual - known == set(), f"new PaddleOCRVL.{label} arguments: {sorted(actual - known)}"
        assert set(allowed) - actual == set(), (
            f"{label} arguments PaddleOCR no longer has: {sorted(set(allowed) - actual)}"
        )
    assert set(SERVICE_SETTING_ARGUMENTS) <= init
    assert set(NAMED_ARGUMENTS) <= init and set(NAMED_ARGUMENTS) <= predict  # a feature is set in both places
    assert set(NOT_EXPOSED) <= init | predict
    assert not set(inspect.signature(PaddleOCRVL.predict_iter).parameters) - set(
        inspect.signature(PaddleOCRVL.predict).parameters
    )


def test_backends_and_pipeline_versions_match_the_installed_engine() -> None:
    import inspect

    from paddleocr import PaddleOCRVL
    from paddleocr._pipelines import paddleocr_vl as module  # the module-level lists are private names

    from paddleocr_vl_shim.engine import BACKENDS, DEFAULT_PIPELINE_VERSION, PIPELINE_VERSIONS

    assert list(BACKENDS) == module._SUPPORTED_VL_BACKENDS
    assert list(PIPELINE_VERSIONS) == module._AVAILABLE_PIPELINE_VERSIONS
    assert inspect.signature(PaddleOCRVL.__init__).parameters["pipeline_version"].default == DEFAULT_PIPELINE_VERSION


@pytest.mark.parametrize("model", ["PP-DocLayoutV3", "PP-DocLayoutV2"])
def test_layout_labels_match_the_layout_models(model: str) -> None:
    """The labels in ``convert.LAYOUT_LABELS`` are the ``label_list`` of the layout models' ``inference.yml``
    (downloaded to ``~/.paddlex/official_models`` by the first parse with that pipeline version)."""
    import yaml

    from paddleocr_vl_shim.convert import LAYOUT_LABELS

    path = (
        Path(os.environ.get("PADDLE_PDX_CACHE_HOME", Path.home() / ".paddlex"))
        / "official_models"
        / model
        / "inference.yml"
    )
    if not path.exists():
        pytest.skip(f"{path} is not downloaded")
    assert tuple(yaml.safe_load(path.read_text())["label_list"]) == LAYOUT_LABELS
