"""The guide's example shim (example/tesseract_shim.py) works, with Tesseract and Pillow stubbed."""

from __future__ import annotations

import importlib.util
import io
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

EXAMPLE = Path(__file__).resolve().parent.parent / "example" / "tesseract_shim.py"
GUIDE = Path(__file__).resolve().parents[3] / "docs" / "protocols" / "parser-shim-guide.md"


class FakeImage:
    width, height = 800, 600


def load(monkeypatch: pytest.MonkeyPatch, words: dict[str, list[Any]]) -> Any:
    seen: dict[str, Any] = {}
    tesseract = types.ModuleType("pytesseract")
    tesseract.Output = types.SimpleNamespace(DICT="dict")  # type: ignore[attr-defined]
    tesseract.get_tesseract_version = lambda: "5.3.0"  # type: ignore[attr-defined]

    def image_to_data(image: Any, lang: str, output_type: str) -> dict[str, list[Any]]:
        seen["lang"] = lang
        return words

    tesseract.image_to_data = image_to_data  # type: ignore[attr-defined]
    pil = types.ModuleType("PIL")
    pil_image = types.ModuleType("PIL.Image")
    pil_image.open = lambda stream: FakeImage()  # type: ignore[attr-defined]
    pil.Image = pil_image  # type: ignore[attr-defined]
    for module in (tesseract, pil, pil_image):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.delenv("TESSERACT_SHIM_API_KEY", raising=False)
    spec = importlib.util.spec_from_file_location("tesseract_shim_example", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seen = seen
    return module


WORDS = {
    "text": ["", "Pump", "Manual", "Mount", "the", "pump", "  "],
    "block_num": [1, 1, 1, 2, 2, 2, 2],
    "par_num": [1, 1, 1, 1, 1, 1, 1],
    "left": [0, 10, 80, 10, 60, 100, 0],
    "top": [0, 10, 10, 50, 50, 50, 0],
    "width": [0, 60, 70, 40, 30, 50, 0],
    "height": [0, 20, 20, 20, 20, 20, 0],
}


def test_the_example_turns_words_into_paragraph_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load(monkeypatch, WORDS)
    client = TestClient(module.app)
    info = client.get("/info").json()
    assert info["name"] == "tesseract-shim" and info["capabilities"]["async"] is True
    assert info["options_schema"]["properties"]["lang"]["default"] == "eng"
    resp = client.post(
        "/parse",
        files={"file": ("scan.png", io.BytesIO(b"png"), "image/png")},
        data={"options": '{"lang": "eng+deu"}'},
    )
    assert resp.status_code == 200, resp.text
    ir = resp.json()
    assert module.seen["lang"] == "eng+deu"
    assert [(b["text"], b["page"], b["bbox"]) for b in ir["blocks"]] == [
        ("Pump Manual", 1, [10, 10, 150, 30]),
        ("Mount the pump", 1, [10, 50, 150, 70]),
    ]
    assert ir["pages"] == [{"page": 1, "width": 800, "height": 600}]


def test_the_example_rejects_what_it_cannot_read_and_typos(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(load(monkeypatch, WORDS).app)
    pdf = client.post("/parse", files={"file": ("a.pdf", b"%PDF", "application/pdf")})
    assert pdf.status_code == 415
    typo = client.post("/parse", files={"file": ("a.png", b"x", "image/png")}, data={"options": '{"lng": "eng"}'})
    assert typo.status_code == 400 and "lng" in typo.json()["error"]["message"]


def test_the_guide_shows_the_example_as_it_is() -> None:
    """The guide embeds the file; this keeps the two from drifting apart."""
    if not GUIDE.exists():
        pytest.skip("the guide is in the RAGCentre repository's docs/")
    source = EXAMPLE.read_text(encoding="utf-8")
    body = source.split('"""', 2)[2].strip()  # the code after the module docstring
    assert body in GUIDE.read_text(encoding="utf-8")
