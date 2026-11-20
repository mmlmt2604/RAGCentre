"""Tests for the EasyOCR shim's service. They need only fastapi, httpx and pytest: no EasyOCR, no
models. The engine, with a stand-in for EasyOCR, is tested in test_engine.py."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shimkit import ShimError

from easyocr_shim.app import NAME, SHIM_VERSION, create_app, service_version

GOLDEN = Path(__file__).parent / "golden" / "sample_ir.json"


def line(text: str, bbox: list[float], confidence: float = 0.98) -> dict[str, Any]:
    return {"text": text, "bbox": bbox, "confidence": confidence}


def sample() -> list[dict[str, Any]]:
    """The line pages ``engine.reduce_page`` produces, describing the two-page "Pump Manual". Pixel
    boxes on a 1200 x 1600 page; EasyOCR split two lines into pieces, as it does at wide gaps."""
    return [
        {
            "page": 1,
            "width": 1200,
            "height": 1600,
            "image": {"mime": "image/png", "data_b64": "AAAA"},
            "lines": [
                line("Pump Manual", [100, 80, 600, 136], 0.99),
                line("Installation", [100, 200, 400, 240]),
                line("Mount the pump on", [100, 280, 520, 304], 0.97),
                line("a level surface.", [530, 280, 1100, 304], 0.95),
                line("Close the valve.", [120, 360, 600, 384]),
                line("  ", [100, 400, 200, 410]),  # blank: dropped
            ],
        },
        {
            "page": 2,
            "width": 1200,
            "height": 1600,
            "image": None,
            "lines": [
                line("Specifications", [100, 80, 500, 112]),
                line("Flow", [100, 160, 200, 184]),
                line("40 l/min", [600, 160, 750, 184]),  # a table column: its own line
                line("Rated pressure 6 bar and", [100, 220, 700, 244]),
                line("a maximum flow of 40 l/min.", [100, 250, 680, 274]),
                line("smudge", [900, 900, 960, 924], 0.12),
            ],
        },
    ]


def fake_engine(calls: list[tuple[Any, ...]]) -> Any:
    def run(content: bytes, filename: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append((content, filename, options))
        if filename.endswith(".bad.pdf"):
            raise RuntimeError("broken file")
        if filename.endswith(".down.pdf"):
            raise ShimError(503, "engine_unavailable", "cannot start EasyOCR")
        return sample()

    return run


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("EASYOCR_SHIM_API_KEY", "EASYOCR_SHIM_LANGS", "EASYOCR_SHIM_GPU"):
        monkeypatch.delenv(name, raising=False)


def post(client: TestClient, name: str = "m.pdf", options: Any = None) -> Any:
    data = {} if options is None else {"options": options if isinstance(options, str) else json.dumps(options)}
    return client.post("/parse", files={"file": (name, b"%PDF", "application/pdf")}, data=data)


DEFAULTS = {
    "languages": None,
    "dpi": 200,
    "page_range": None,
    "paragraphs": True,
    "headings": False,
    "min_confidence": 0,
    "decoder": "greedy",
    "rotated_text": False,
    "canvas_size": 2560,
    "include_page_images": False,
}


def test_service_endpoints() -> None:
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_engine(calls)))
    info = client.get("/info").json()
    assert info["protocol"] == "ragcentre.parser/v1" and info["name"] == NAME == "easyocr-shim"
    assert info["version"].startswith(f"{SHIM_VERSION}+easyocr.")
    assert info["mime_types"] == ["application/pdf", "image/png", "image/jpeg", "image/bmp", "image/tiff", "image/webp"]
    assert info["capabilities"] == {"page_images": True, "async": True}

    resp = post(client, options={"languages": ["en", "de"], "headings": True})
    assert resp.status_code == 200
    ir = resp.json()
    assert ir["metadata"] == {"page_count": 2, "title": "Pump Manual", "source_name": "m.pdf"}
    assert calls[0] == (b"%PDF", "m.pdf", {**DEFAULTS, "languages": ["en", "de"], "headings": True})
    assert post(client).status_code == 200
    assert calls[1][2] == DEFAULTS

    bad = post(client, options='["not an object"]')
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_options"
    for wrong in ({"dpi": 10}, {"decoder": "magic"}, {"languages": "en"}, {"min_confidence": 101}, {"typo": 1}):
        assert post(client, options=wrong).status_code == 400, wrong
    unsupported = post(client, name="notes.txt")
    assert unsupported.status_code == 415 and unsupported.json()["error"]["code"] == "unsupported_media_type"
    assert len(calls) == 2  # the 400s and the 415 never reached the engine
    broken = post(client, name="m.bad.pdf")
    assert broken.status_code == 422 and "broken file" in broken.json()["error"]["message"]
    down = post(client, name="m.down.pdf")
    assert down.status_code == 503 and down.json()["error"]["code"] == "engine_unavailable"


def test_layout_of_the_sample() -> None:
    ir = post(TestClient(create_app(fake_engine([]))), options={"headings": True}).json()
    assert [(b["type"], b.get("level"), b["text"], b["page"]) for b in ir["blocks"]] == [
        ("heading", 1, "Pump Manual", 1),
        ("heading", 2, "Installation", 1),
        ("paragraph", None, "Mount the pump on a level surface.", 1),
        ("paragraph", None, "Close the valve.", 1),
        ("heading", 3, "Specifications", 2),
        ("paragraph", None, "Flow", 2),
        ("paragraph", None, "40 l/min", 2),
        ("paragraph", None, "Rated pressure 6 bar and a maximum flow of 40 l/min.", 2),
        ("paragraph", None, "smudge", 2),
    ]
    mount = ir["blocks"][2]
    assert mount["bbox"] == [100.0, 280.0, 1100.0, 304.0] and mount["metadata"] == {"confidence": 0.96}
    assert "image" not in ir["pages"][0]


def test_options_reach_the_layout() -> None:
    client = TestClient(create_app(fake_engine([])))
    plain = post(client).json()["blocks"]
    assert {b["type"] for b in plain} == {"paragraph"}  # headings are opt-in
    lines = post(client, options={"paragraphs": False}).json()["blocks"]
    assert "Rated pressure 6 bar and" in [b["text"] for b in lines]
    confident = post(client, options={"min_confidence": 50}).json()["blocks"]
    assert "smudge" not in [b["text"] for b in confident]
    with_images = post(client, options={"include_page_images": True}).json()
    assert with_images["pages"][0]["image"] == {"mime": "image/png", "data_b64": "AAAA"}


def test_matches_golden() -> None:
    """``tests/golden/sample_ir.json`` is this sample's IR with ``headings`` on. RAGCentre's own test
    suite validates the same file (tests/test_parser_shim_goldens.py), since the two cannot share an
    environment. Regenerate with ``UPDATE_GOLDEN=1 uv run pytest``."""
    got = post(TestClient(create_app(fake_engine([]))), name="manual.pdf", options={"headings": True}).json()
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=2) + "\n", encoding="utf-8")
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name", ["scan.PNG", "scan.jpg", "scan.jpeg", "scan.bmp", "scan.tif", "scan.TIFF", "scan.webp"]
)
def test_image_extensions_are_accepted(name: str) -> None:
    assert post(TestClient(create_app(fake_engine([]))), name=name).status_code == 200


@pytest.mark.parametrize("name", ["a.docx", "a.html", "a.gif", "a", "a.pdf.exe"])
def test_other_extensions_are_415(name: str) -> None:
    assert post(TestClient(create_app(fake_engine([]))), name=name).status_code == 415


def test_default_languages_are_part_of_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ".lang-" not in service_version()
    monkeypatch.setenv("EASYOCR_SHIM_LANGS", "en, ch_sim")
    info = TestClient(create_app(fake_engine([]))).get("/info").json()
    assert info["version"].endswith(".lang-en+ch_sim")


def test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EASYOCR_SHIM_API_KEY", "s3cret")
    client = TestClient(create_app(fake_engine([])))
    assert client.get("/info").status_code == 200  # RAGCentre reads /info without credentials
    assert post(client).status_code == 401
    ok = client.post(
        "/parse", files={"file": ("m.pdf", b"x", "application/pdf")}, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


def test_options_are_published() -> None:
    schema = TestClient(create_app(fake_engine([]))).get("/info").json()["options_schema"]
    assert schema["additionalProperties"] is False
    assert all("description" in spec for spec in schema["properties"].values())
    assert schema["properties"]["languages"] == {
        "type": "array",
        "items": {"type": "string"},
        "description": schema["properties"]["languages"]["description"],
    }
