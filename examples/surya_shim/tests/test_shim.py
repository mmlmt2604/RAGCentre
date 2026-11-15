"""Tests for the Surya shim. They need only fastapi, httpx and pytest: no Surya, no models, no Pillow."""

from __future__ import annotations

import base64
import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shimkit import ShimError

from surya_shim import app as app_module
from surya_shim.app import NAME, create_app, service_version
from surya_shim.convert import to_ir
from surya_shim.engine import SuryaEngine, encode_png, reduce_page

GOLDEN = Path(__file__).parent / "golden" / "sample_ir.json"
PNG_BYTES = b"\x89PNG fake"


def block(
    label: str,
    html: str,
    bbox: list[float],
    order: int,
    *,
    skipped: bool = False,
    error: bool = False,
    confidence: float | None = None,
) -> dict[str, Any]:
    return {
        "label": label,
        "html": html,
        "bbox": bbox,
        "confidence": confidence,
        "reading_order": order,
        "skipped": skipped,
        "error": error,
    }


def sample() -> list[dict[str, Any]]:
    """The reduced shape ``engine.reduce_page`` produces (see convert.py for the engine source it
    follows), describing the two-page "Pump Manual". Pixel boxes on a 1200 x 1600 page. Blocks are
    given out of reading order on purpose."""
    return [
        {
            "page": 1,
            "width": 1200,
            "height": 1600,
            "image": {"mime": "image/png", "data_b64": "AAAA"},
            "blocks": [
                block("Text", "<p>Mount the pump on a level surface.</p>", [100, 280, 1100, 320], 2, confidence=0.91),
                block("SectionHeader", "<h2><b>Installation</b></h2>", [100, 200, 400, 240], 1),
                block("SectionHeader", "<h1><b>Pump Manual</b></h1>", [100, 80, 600, 140], 0, confidence=0.99),
                block("ListGroup", "<ul><li>Close the valve.</li></ul>", [120, 340, 600, 370], 3, confidence=0.5),
                block("Text", "<p> </p>", [100, 400, 200, 410], 4),  # blank: dropped
            ],
        },
        {
            "page": 2,
            "width": 1200,
            "height": 1600,
            "image": {"mime": "image/png", "data_b64": "BBBB"},
            "blocks": [
                block("SectionHeader", "<h3>Specifications</h3>", [100, 80, 500, 120], 0),
                block(
                    "Table",
                    "<table><tr><th><b>Flow</b></th></tr><tr><td>40 l/min</td></tr></table>",
                    [100, 160, 600, 400],
                    1,
                ),
                block("Picture", "", [100, 440, 600, 900], 2, skipped=True),
                block("Caption", "<p>Figure 1: exploded view</p>", [100, 920, 600, 960], 3),
            ],
        },
    ]


def test_mapping() -> None:
    ir = to_ir(sample(), source_name="manual.pdf")
    assert ir["schema_version"] == "1.1"
    assert ir["metadata"] == {"page_count": 2, "title": "Pump Manual", "source_name": "manual.pdf"}
    assert ir["pages"] == [
        {"page": 1, "width": 1200.0, "height": 1600.0},
        {"page": 2, "width": 1200.0, "height": 1600.0},
    ]
    kinds = [(b["type"], b.get("level"), b["text"]) for b in ir["blocks"]]
    assert kinds[:5] == [
        ("heading", 1, "Pump Manual"),
        ("heading", 2, "Installation"),
        ("paragraph", None, "Mount the pump on a level surface."),
        ("list_item", None, "Close the valve."),
        ("heading", 3, "Specifications"),
    ]
    table = next(b for b in ir["blocks"] if b["type"] == "table")
    assert table["text"] == "| Flow |\n| --- |\n| 40 l/min |"
    assert table["html"] == "<table><tr><th><b>Flow</b></th></tr><tr><td>40 l/min</td></tr></table>"
    figure = next(b for b in ir["blocks"] if b["type"] == "figure")
    caption = next(b for b in ir["blocks"] if b["type"] == "caption")
    assert figure["text"] == "" and figure["page"] == 2
    assert caption["text"] == "Figure 1: exploded view" and caption["page"] == 2
    assert [b["id"] for b in ir["blocks"]] == [f"b{i}" for i in range(1, len(ir["blocks"]) + 1)]
    assert all(b["text"].strip() or b["type"] == "figure" for b in ir["blocks"])


def test_confidence_is_carried_to_every_block_made_from_a_surya_block() -> None:
    blocks = {b["text"]: b for b in to_ir(sample())["blocks"]}
    assert blocks["Pump Manual"]["confidence"] == 0.99
    assert blocks["Mount the pump on a level surface."]["confidence"] == 0.91
    assert blocks["Close the valve."]["confidence"] == 0.5
    two_items = [
        {
            "page": 1,
            "width": 10,
            "height": 10,
            "blocks": [block("ListGroup", "<ul><li>a</li><li>b</li></ul>", [0, 0, 1, 1], 0, confidence=0.7)],
        }
    ]
    assert [b["confidence"] for b in to_ir(two_items)["blocks"]] == [0.7, 0.7]
    assert "confidence" not in blocks["Installation"]  # Surya gave none: left out, not made up


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0.0),
        (1, 1.0),
        (0.25, 0.25),
        (1.0000001, 1.0),
        (-0.1, 0.0),
        (None, None),
        ("0.9", None),
        (True, None),
        (float("nan"), None),
    ],
)
def test_confidence_is_clamped_to_the_irs_range_or_left_out(value: Any, expected: float | None) -> None:
    pages = [
        {"page": 1, "width": 10, "height": 10, "blocks": [block("Text", "<p>x</p>", [0, 0, 1, 1], 0, confidence=value)]}
    ]
    got = to_ir(pages)["blocks"][0].get("confidence")
    assert got == expected


def test_blocks_are_in_reading_order() -> None:
    first_page = [b["text"] for b in to_ir(sample())["blocks"] if b["page"] == 1]
    assert first_page == ["Pump Manual", "Installation", "Mount the pump on a level surface.", "Close the valve."]


def test_bbox_units() -> None:
    """Boxes are the pixels of the rendered page, and so are the page sizes: nothing is scaled."""
    ir = to_ir(sample())
    assert ir["blocks"][0]["bbox"] == [100.0, 80.0, 600.0, 140.0]
    for b in ir["blocks"]:
        page = next(p for p in ir["pages"] if p["page"] == b["page"])
        x0, y0, x1, y1 = b["bbox"]
        assert 0 <= x0 < x1 <= page["width"] and 0 <= y0 < y1 <= page["height"]


def test_page_images_only_when_asked() -> None:
    assert all("image" not in p for p in to_ir(sample())["pages"])
    with_images = to_ir(sample(), include_page_images=True)["pages"]
    assert [p["image"] for p in with_images] == [
        {"mime": "image/png", "data_b64": "AAAA"},
        {"mime": "image/png", "data_b64": "BBBB"},
    ]
    no_image = [{"page": 1, "width": 1, "height": 1, "blocks": [], "image": None}]
    assert "image" not in to_ir(no_image, include_page_images=True)["pages"][0]


def one(label: str, html: str) -> list[dict[str, Any]]:
    return to_ir([{"page": 1, "width": 10, "height": 10, "blocks": [block(label, html, [1, 2, 3, 4], 0)]}])["blocks"]


@pytest.mark.parametrize(
    ("label", "html", "expected"),
    [
        ("SectionHeader", "<h1>T</h1>", {"type": "heading", "level": 1, "text": "T"}),
        ("SectionHeader", "<h4><b>Deep</b></h4>", {"type": "heading", "level": 4, "text": "Deep"}),
        ("SectionHeader", "<h6>x</h6>", {"type": "heading", "level": 6, "text": "x"}),
        ("SectionHeader", "<p>no tag</p>", {"type": "heading", "level": 2, "text": "no tag"}),
        ("SectionHeader", "plain", {"type": "heading", "level": 2, "text": "plain"}),
        ("Text", "<p>a <b>b</b></p>", {"type": "paragraph", "text": "a b"}),
        ("Bibliography", "<p>[1] Ref</p>", {"type": "paragraph", "text": "[1] Ref"}),
        (
            "Form",
            "<p>Name: ____</p>",
            {"type": "paragraph", "text": "Name: ____", "metadata": {"original_type": "Form"}},
        ),
        (
            "ChemicalBlock",
            "<p>H2O</p>",
            {"type": "paragraph", "text": "H2O", "metadata": {"original_type": "ChemicalBlock"}},
        ),
        ("Equation", "<math>E = mc^2</math>", {"type": "formula", "text": "E = mc^2"}),
        ("Equation", "<p><math>a_1</math></p><p><math>b_2</math></p>", {"type": "formula", "text": "a_1\nb_2"}),
        ("Code", "<pre><code>if x:\n    y()\n</code></pre>", {"type": "code", "text": "if x:\n    y()"}),
        ("Picture", "", {"type": "figure", "text": ""}),
        ("Figure", "<p>chart text</p>", {"type": "figure", "text": "chart text"}),
        ("Diagram", "", {"type": "figure", "text": ""}),
        ("Caption", "<p>Fig</p>", {"type": "caption", "text": "Fig"}),
        ("Footnote", "<p>1 Note</p>", {"type": "footnote", "text": "1 Note"}),
        ("PageHeader", "<p>Acme</p>", {"type": "page_header", "text": "Acme"}),
        ("PageFooter", "<p>Page 3</p>", {"type": "page_footer", "text": "Page 3"}),
        ("SomethingNew", "<p>x</p>", {"type": "paragraph", "text": "x", "metadata": {"original_type": "SomethingNew"}}),
    ],
)
def test_label_mapping(label: str, html: str, expected: dict[str, Any]) -> None:
    (got,) = one(label, html)
    assert {k: got[k] for k in expected} == expected
    assert got["page"] == 1 and got["bbox"] == [1.0, 2.0, 3.0, 4.0]


@pytest.mark.parametrize("label", ["ListGroup", "TableOfContents"])
def test_lists_become_one_list_item_per_li(label: str) -> None:
    blocks = one(label, "<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul>")
    assert [(b["type"], b["text"]) for b in blocks] == [
        ("list_item", "one"),
        ("list_item", "two"),
        ("list_item", "nested"),
    ]
    assert all(b["page"] == 1 and b["bbox"] == [1.0, 2.0, 3.0, 4.0] for b in blocks)


def test_a_list_without_li_is_a_paragraph() -> None:
    (b,) = one("ListGroup", "<p>1. first 2. second</p>")
    assert (b["type"], b["text"]) == ("paragraph", "1. first 2. second")


def test_table_without_a_table_tag_keeps_the_text() -> None:
    (b,) = one("Table", "<p>Flow 40</p>")
    assert b["type"] == "table" and b["text"] == "Flow 40" and "html" not in b


def test_table_spans_are_expanded() -> None:
    html = "<table><tr><th colspan='2'>Pump</th></tr><tr><td>a</td><td>b</td></tr></table>"
    (b,) = one("Table", html)
    assert b["text"] == "| Pump | Pump |\n| --- | --- |\n| a | b |" and b["html"] == html


def test_skipped_errored_and_blank_page_blocks() -> None:
    page = {
        "page": 1,
        "width": 10,
        "height": 10,
        "blocks": [
            block("Text", "<p>kept</p>", [0, 0, 1, 1], 0),
            block("Text", "<p>failed</p>", [0, 0, 1, 1], 1, error=True),
            block("BlankPage", "", [0, 0, 1, 1], 2),
            block("Picture", "", [0, 0, 1, 1], 3, skipped=True),
        ],
    }
    assert [(b["type"], b["text"]) for b in to_ir([page])["blocks"]] == [("paragraph", "kept"), ("figure", "")]


def test_no_level_one_heading_is_promoted() -> None:
    """Surya has no document-title label: with no ``<h1>`` there is no level-1 heading and no title."""
    pages = [
        {"page": 1, "width": 10, "height": 10, "blocks": [block("SectionHeader", "<h2>Intro</h2>", [0, 0, 1, 1], 0)]}
    ]
    ir = to_ir(pages)
    assert [(b["type"], b["level"]) for b in ir["blocks"]] == [("heading", 2)]
    assert "title" not in ir["metadata"]


def test_matches_golden() -> None:
    """``tests/golden/sample_ir.json`` is this sample's IR. RAGCentre's own test suite validates the
    same file (tests/test_parser_shim_goldens.py), since the two cannot share an environment.
    Regenerate with ``UPDATE_GOLDEN=1 uv run pytest``."""
    got = to_ir(sample(), source_name="manual.pdf")
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=2) + "\n", encoding="utf-8")
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))


# --- service ----------------------------------------------------------------------------------


def fake_engine(calls: list[tuple[Any, ...]]) -> Any:
    def run(content: bytes, filename: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append((content, filename, options))
        if filename.endswith(".bad.pdf"):
            raise RuntimeError("broken file")
        if filename.endswith(".down.pdf"):
            raise ShimError(503, "engine_unavailable", "inference server unreachable")
        return sample()

    return run


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SURYA_SHIM_API_KEY", raising=False)


def post(client: TestClient, name: str = "m.pdf", options: Any = None) -> Any:
    data = {} if options is None else {"options": options if isinstance(options, str) else json.dumps(options)}
    return client.post("/parse", files={"file": (name, b"%PDF", "application/pdf")}, data=data)


def test_service_endpoints() -> None:
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_engine(calls)))
    info = client.get("/info").json()
    assert info["protocol"] == "ragcentre.parser/v1" and info["name"] == NAME == "surya-shim"
    assert info["version"].startswith("0.3.0+surya.")
    assert info["mime_types"] == ["application/pdf", "image/png", "image/jpeg", "image/tiff", "image/bmp", "image/webp"]
    assert info["capabilities"] == {"page_images": True}

    resp = post(client, options={"include_page_images": True, "dpi": 150, "ocr_figures": True})
    assert resp.status_code == 200
    ir = resp.json()
    assert ir["blocks"][0]["text"] == "Pump Manual" and ir["pages"][0]["image"]["mime"] == "image/png"
    assert calls[0] == (
        b"%PDF",
        "m.pdf",
        {"include_page_images": True, "ocr_figures": True, "dpi": 150, "page_range": None},
    )
    plain = post(client)
    assert plain.status_code == 200 and "image" not in plain.json()["pages"][0]
    assert calls[1][2] == {"include_page_images": False, "ocr_figures": False, "dpi": 192, "page_range": None}

    bad = post(client, options="[1]")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_options"
    for options in (
        {"dpi": 71},
        {"dpi": 401},
        {"dpi": "192"},
        {"dpi": True},
        {"include_page_images": "yes"},
        {"ocr_figures": 1},
    ):
        assert post(client, options=options).status_code == 400, options
    unsupported = post(client, name="notes.docx")
    assert unsupported.status_code == 415 and unsupported.json()["error"]["code"] == "unsupported_media_type"
    assert len(calls) == 2  # the 400s and the 415 never reached the engine
    broken = post(client, name="m.bad.pdf")
    assert broken.status_code == 422 and "broken file" in broken.json()["error"]["message"]
    down = post(client, name="m.down.pdf")
    assert down.status_code == 503 and down.json()["error"]["code"] == "engine_unavailable"


@pytest.mark.parametrize("name", ["a.PNG", "a.jpg", "a.jpeg", "a.tif", "a.tiff", "a.bmp", "a.webp", "a.PDF"])
def test_supported_extensions(name: str) -> None:
    assert post(TestClient(create_app(fake_engine([]))), name=name).status_code == 200


@pytest.mark.parametrize("name", ["a.docx", "a.html", "a.gif", "a", "a.txt"])
def test_other_extensions_are_415(name: str) -> None:
    assert post(TestClient(create_app(fake_engine([]))), name=name).status_code == 415


def test_version_includes_the_package_and_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = types.ModuleType("surya.settings")
    settings.settings = types.SimpleNamespace(SURYA_MODEL_CHECKPOINT="datalab-to/surya-ocr-2.5")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "surya.settings", settings)
    monkeypatch.setattr(app_module, "version", lambda name: "0.24.0")
    assert service_version() == "0.3.0+surya.0.24.0.surya-ocr-2.5"
    settings.settings.SURYA_MODEL_CHECKPOINT = "/models/my-finetune/"
    assert service_version() == "0.3.0+surya.0.24.0.my-finetune"


def fake_surya(monkeypatch: pytest.MonkeyPatch, *, autodetect: Any = "llamacpp", **settings_values: Any) -> Any:
    values: dict[str, Any] = {
        "SURYA_MODEL_CHECKPOINT": "datalab-to/surya-ocr-2.5",
        "SURYA_INFERENCE_BACKEND": None,
        "SURYA_GGUF_MODEL_FILE": "surya-2.5.gguf",
        "SURYA_GGUF_LOCAL_MODEL_PATH": None,
        "SURYA_GGUF_LOCAL_MMPROJ_PATH": None,
    }
    values.update(settings_values)
    settings = types.ModuleType("surya.settings")
    settings.settings = types.SimpleNamespace(**values)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "surya.settings", settings)
    inference = types.ModuleType("surya.inference")
    if autodetect is not None:
        inference._autodetect_backend = lambda: autodetect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "surya.inference", inference)
    monkeypatch.setattr(app_module, "version", lambda name: "0.24.0")
    return settings.settings


def test_version_names_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = fake_surya(monkeypatch, autodetect="vllm")
    assert service_version() == "0.3.0+surya.0.24.0.surya-ocr-2.5.vllm"  # auto-detected
    settings.SURYA_INFERENCE_BACKEND = "MLX"
    assert service_version() == "0.3.0+surya.0.24.0.surya-ocr-2.5.mlx"  # the setting wins


def test_version_backend_falls_back_to_auto_when_detection_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_surya(monkeypatch, autodetect=None)
    assert service_version() == "0.3.0+surya.0.24.0.surya-ocr-2.5.auto"


def test_version_names_a_non_default_gguf_file(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = fake_surya(monkeypatch)
    assert service_version() == "0.3.0+surya.0.24.0.surya-ocr-2.5.llamacpp"  # the default file adds nothing
    settings.SURYA_GGUF_MODEL_FILE = "surya-2.5-q4.gguf"
    assert service_version().endswith(".llamacpp.gguf-surya-2-5-q4-gguf")
    # A local path counts only with its mmproj, as in surya/inference/backends/llamacpp.py.
    settings.SURYA_GGUF_LOCAL_MODEL_PATH = "/models/my tune.gguf"
    assert service_version().endswith(".gguf-surya-2-5-q4-gguf")
    settings.SURYA_GGUF_LOCAL_MMPROJ_PATH = "/models/mmproj.gguf"
    assert service_version().endswith(".llamacpp.gguf-my-tune-gguf")


def test_gguf_file_is_ignored_for_other_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_surya(monkeypatch, autodetect="vllm", SURYA_GGUF_MODEL_FILE="other.gguf")
    assert "gguf" not in service_version()


def test_unknown_option_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    client = TestClient(create_app(fake_engine(calls)))
    for options in ({"ocr_figurs": True}, {"dpi": "150"}, {"dpi": 401}, {"include_page_images": 1}):
        resp = post(client, options=json.dumps(options))
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_options", options
    assert post(client, options=json.dumps({"ocr_figurs": True})).json()["error"]["message"] == (
        "option 'ocr_figurs' is not recognised"
    )
    assert calls == []


def test_info_reports_the_options_schema() -> None:
    schema = TestClient(create_app(fake_engine([]))).get("/info").json()["options_schema"]
    assert set(schema["properties"]) == {"include_page_images", "ocr_figures", "dpi", "page_range"}
    assert schema["additionalProperties"] is False


def test_version_without_surya(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "surya.settings", None)  # makes the import fail
    monkeypatch.setattr(
        app_module, "version", lambda name: (_ for _ in ()).throw(app_module.PackageNotFoundError(name))
    )
    assert service_version() == "0.3.0+surya.unknown"


def test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SURYA_SHIM_API_KEY", "s3cret")
    client = TestClient(create_app(fake_engine([])))
    assert client.get("/info").status_code == 200  # RAGCentre reads /info without credentials
    assert post(client).status_code == 401
    ok = client.post(
        "/parse", files={"file": ("m.pdf", b"x", "application/pdf")}, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


# --- engine (with a stand-in for the surya package) ----------------------------------------------


class FakeImage:
    def __init__(self, size: tuple[int, int]) -> None:
        self.size = size

    def save(self, fp: Any, format: str) -> None:
        assert format == "PNG"
        fp.write(PNG_BYTES)


def prediction(width: float = 1632, height: float = 2112) -> types.SimpleNamespace:
    blocks = [
        types.SimpleNamespace(
            label="Text",
            html="<p>hi</p>",
            bbox=[1.5, 2, 3, 4],
            confidence=0.93,
            reading_order=0,
            skipped=False,
            error=False,
        )
    ]
    return types.SimpleNamespace(image_bbox=[0.0, 0.0, width, height], blocks=blocks)


class Env:
    """The recorded calls of the fake surya modules."""

    def __init__(self) -> None:
        self.manager_created = 0
        self.manager_stopped = 0
        self.predict_calls: list[tuple[list[Any], dict[str, Any]]] = []
        self.load_calls: list[tuple[str, Path, Any, bytes]] = []
        self.page_ranges: list[list[int] | None] = []
        self.fail_manager: Exception | None = None
        self.fail_predict: Exception | None = None


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    state = Env()

    class Manager:
        def __init__(self) -> None:
            if state.fail_manager:
                raise state.fail_manager
            state.manager_created += 1

        def stop(self) -> None:
            state.manager_stopped += 1

    class Predictor:
        def __init__(self, manager: Manager) -> None:
            self.manager = manager

        def __call__(self, images: list[Any], **kwargs: Any) -> list[Any]:
            state.predict_calls.append((images, kwargs))
            if state.fail_predict:
                raise state.fail_predict
            return [prediction() for _ in images]

    def load_pdf(path: Path, dpi: int, page_range: list[int] | None = None) -> tuple[list[Any], list[str]]:
        # Like surya.input.load.load_pdf: 0-based indices, an error for any outside the file (5 pages).
        state.load_calls.append(("pdf", path, dpi, path.read_bytes()))
        state.page_ranges.append(page_range)
        if page_range is None:
            return [FakeImage((1632, 2112)), FakeImage((1632, 2112))], ["a", "a"]
        if not all(0 <= page < 5 for page in page_range):
            raise ValueError(f"Invalid page range {page_range} for PDF with 5 page(s).")
        return [FakeImage((1632, 2112)) for _ in page_range], ["a"] * len(page_range)

    def load_image(path: Path) -> tuple[list[Any], list[str]]:
        state.load_calls.append(("image", path, None, path.read_bytes()))
        return [FakeImage((800, 600))], ["a"]

    surya = types.ModuleType("surya")
    surya.SuryaInferenceManager = Manager  # type: ignore[attr-defined]
    surya.RecognitionPredictor = Predictor  # type: ignore[attr-defined]
    surya_input = types.ModuleType("surya.input")
    loader = types.ModuleType("surya.input.load")
    loader.load_pdf = load_pdf  # type: ignore[attr-defined]
    loader.load_image = load_image  # type: ignore[attr-defined]
    for module in (surya, surya_input, loader):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return state


OPTIONS = {"include_page_images": False, "ocr_figures": False, "dpi": 192, "page_range": None}


def test_reduce_page_and_encode_png() -> None:
    page = reduce_page(prediction(), 2, FakeImage((1632, 2112)), with_image=True)
    assert page == {
        "page": 2,
        "width": 1632.0,
        "height": 2112.0,
        "blocks": [
            {
                "label": "Text",
                "html": "<p>hi</p>",
                "bbox": [1.5, 2, 3, 4],
                "confidence": 0.93,
                "reading_order": 0,
                "skipped": False,
                "error": False,
            }
        ],
        "image": {"mime": "image/png", "data_b64": base64.b64encode(PNG_BYTES).decode()},
    }
    assert reduce_page(prediction(), 1, FakeImage((1, 1)), with_image=False)["image"] is None
    assert encode_png(FakeImage((1, 1)))["mime"] == "image/png"


def test_engine_renders_a_pdf_at_the_requested_dpi(env: Env) -> None:
    pages = SuryaEngine()(b"%PDF-data", "m.PDF", {**OPTIONS, "dpi": 150})
    ((kind, path, dpi, content),) = env.load_calls
    assert (kind, dpi, content, path.suffix) == ("pdf", 150, b"%PDF-data", ".pdf") and not path.exists()
    assert [p["page"] for p in pages] == [1, 2] and pages[0]["width"] == 1632.0 and pages[0]["image"] is None
    images, kwargs = env.predict_calls[0]
    assert len(images) == 2 and kwargs == {"skip_labels": None}


def test_engine_reads_an_image_with_load_image(env: Env) -> None:
    pages = SuryaEngine()(b"png-bytes", "scan.png", {**OPTIONS, "include_page_images": True})
    ((kind, path, dpi, content),) = env.load_calls
    assert (kind, dpi, content, path.suffix) == ("image", None, b"png-bytes", ".png")
    assert len(pages) == 1 and pages[0]["image"]["mime"] == "image/png"


def test_ocr_figures_clears_the_skip_labels(env: Env) -> None:
    SuryaEngine()(b"x", "a.pdf", {**OPTIONS, "ocr_figures": True})
    assert env.predict_calls[0][1] == {"skip_labels": []}


def test_engine_creates_the_manager_once(env: Env) -> None:
    engine = SuryaEngine()
    engine(b"x", "a.pdf", OPTIONS)
    engine(b"x", "b.pdf", OPTIONS)
    assert env.manager_created == 1 and len(env.predict_calls) == 2


def test_manager_failure_is_503_and_is_retried(env: Env) -> None:
    env.fail_manager = RuntimeError("llama-server not found")
    engine = SuryaEngine()
    with pytest.raises(ShimError) as err:
        engine(b"x", "a.pdf", OPTIONS)
    assert err.value.status == 503 and "llama-server not found" in err.value.message
    env.fail_manager = None
    assert len(engine(b"x", "a.pdf", OPTIONS)) == 2  # a later request starts it


def test_missing_surya_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "surya", None)
    with pytest.raises(ShimError) as err:
        SuryaEngine()(b"x", "a.pdf", OPTIONS)
    assert err.value.status == 503


class APIConnectionError(Exception):
    """Stands in for ``openai.APIConnectionError``, which Surya's backends raise."""


@pytest.mark.parametrize(
    "failure", [APIConnectionError("refused"), ConnectionRefusedError("refused"), TimeoutError("slow")]
)
def test_inference_server_failures_are_503(env: Env, failure: Exception) -> None:
    env.fail_predict = failure
    with pytest.raises(ShimError) as err:
        SuryaEngine()(b"x", "a.pdf", OPTIONS)
    assert err.value.status == 503 and err.value.code == "engine_unavailable"


def test_other_prediction_failures_are_422(env: Env) -> None:
    env.fail_predict = ValueError("page is corrupt")
    resp = post(TestClient(create_app(SuryaEngine())))
    assert resp.status_code == 422 and "page is corrupt" in resp.json()["error"]["message"]


def test_engine_end_to_end_through_the_service(env: Env) -> None:
    resp = post(TestClient(create_app(SuryaEngine())), options={"include_page_images": True})
    ir = resp.json()
    assert resp.status_code == 200 and ir["blocks"][0] == {
        "type": "paragraph",
        "text": "hi",
        "page": 1,
        "bbox": [1.5, 2.0, 3.0, 4.0],
        "confidence": 0.93,
        "id": "b1",
    }
    assert ir["pages"][0]["image"]["mime"] == "image/png"


def test_close_stops_the_inference_server_once(env: Env) -> None:
    engine = SuryaEngine()
    engine.close()  # nothing started yet: nothing to stop
    assert env.manager_stopped == 0
    engine(b"x", "a.pdf", OPTIONS)
    engine.close()
    engine.close()
    assert env.manager_stopped == 1
    engine(b"x", "a.pdf", OPTIONS)  # a later request starts a new one
    assert env.manager_created == 2


def test_the_server_is_stopped_when_the_app_shuts_down(env: Env) -> None:
    with TestClient(create_app(SuryaEngine())) as client:
        assert post(client).status_code == 200
        assert env.manager_stopped == 0
    assert env.manager_stopped == 1


def test_a_failing_stop_does_not_break_shutdown(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = SuryaEngine()
    engine(b"x", "a.pdf", OPTIONS)
    monkeypatch.setattr(engine._manager, "stop", lambda: (_ for _ in ()).throw(RuntimeError("already gone")))
    engine.close()


def test_page_range_loads_zero_based_indices_and_keeps_the_files_page_numbers(env: Env) -> None:
    pages = SuryaEngine()(b"x", "a.pdf", {**OPTIONS, "page_range": [2, 4]})
    assert env.page_ranges == [[1, 2, 3]]  # 1-based inclusive in, 0-based indices out
    assert [p["page"] for p in pages] == [2, 3, 4]
    assert len(env.predict_calls[0][0]) == 3
    pages = SuryaEngine()(b"x", "a.pdf", {**OPTIONS, "page_range": [5, 5]})
    assert env.page_ranges[-1] == [4] and [p["page"] for p in pages] == [5]


def test_without_a_page_range_pages_are_numbered_from_one_and_load_pdf_gets_none(env: Env) -> None:
    pages = SuryaEngine()(b"x", "a.pdf", OPTIONS)
    assert env.page_ranges == [None] and [p["page"] for p in pages] == [1, 2]


def test_a_page_range_outside_the_file_is_400(env: Env) -> None:
    with pytest.raises(ShimError) as err:
        SuryaEngine()(b"x", "a.pdf", {**OPTIONS, "page_range": [4, 7]})
    assert err.value.status == 400 and err.value.code == "invalid_options"
    assert "'page_range' [4, 7]" in err.value.message and "5 page(s)" in err.value.message


def test_page_range_on_an_image(env: Env) -> None:
    assert [p["page"] for p in SuryaEngine()(b"x", "a.png", {**OPTIONS, "page_range": [1, 3]})] == [1]
    with pytest.raises(ShimError) as err:
        SuryaEngine()(b"x", "a.png", {**OPTIONS, "page_range": [2, 3]})
    assert err.value.status == 400 and "1-page image" in err.value.message


def test_page_range_and_the_missing_full_page_option_through_the_service() -> None:
    client = TestClient(create_app(fake_engine([])))
    schema = client.get("/info").json()["options_schema"]["properties"]
    assert "page_range" in schema and "full_page" not in schema
    for bad in ({"page_range": [3, 1]}, {"page_range": [0, 1]}, {"full_page": True}):
        resp = post(client, options=bad)
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_options"
    assert post(client, options={"page_range": [1, 2]}).status_code == 200
