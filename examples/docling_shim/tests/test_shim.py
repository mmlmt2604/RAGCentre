"""Tests for the shim. They need docling-core (the document model) but not Docling's models."""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest
from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    ProvenanceItem,
    Size,
    TableCell,
    TableData,
)
from fastapi.testclient import TestClient

from docling_shim import app as app_module
from docling_shim.app import MAX_CONVERTERS, PROTOCOL, DoclingConverter, Options, create_app, service_version
from docling_shim.convert import to_ir

GOLDEN = Path(__file__).parent / "golden" / "sample_ir.json"


def prov(page: int, l: float, t: float, r: float, b: float) -> ProvenanceItem:  # noqa: E741
    # Docling PDF boxes use a bottom-left origin.
    return ProvenanceItem(
        page_no=page, bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.BOTTOMLEFT), charspan=(0, 1)
    )


def sample() -> DoclingDocument:
    doc = DoclingDocument(name="manual")
    doc.add_page(page_no=1, size=Size(width=600, height=800))
    doc.add_page(page_no=2, size=Size(width=600, height=800))
    doc.add_title("Pump Manual", prov=prov(1, 50, 760, 300, 740))
    doc.add_heading("Installation", level=1, prov=prov(1, 50, 700, 200, 680))
    doc.add_text(DocItemLabel.TEXT, "Mount the pump on a level surface.", prov=prov(1, 50, 660, 550, 640))
    group = doc.add_list_group()
    doc.add_list_item("Close the valve.", parent=group, prov=prov(1, 60, 620, 300, 605))
    doc.add_heading("Specifications", level=2, prov=prov(2, 50, 760, 250, 740))
    cells = [
        TableCell(text="Flow", start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=0,
                  end_col_offset_idx=1, column_header=True),
        TableCell(text="40 l/min", start_row_offset_idx=1, end_row_offset_idx=2, start_col_offset_idx=0,
                  end_col_offset_idx=1),
    ]  # fmt: skip
    doc.add_table(data=TableData(num_rows=2, num_cols=1, table_cells=cells), prov=prov(2, 50, 700, 300, 600))
    caption = doc.add_text(DocItemLabel.CAPTION, "Figure 1: exploded view", prov=prov(2, 50, 400, 300, 380))
    doc.add_picture(caption=caption, prov=prov(2, 50, 580, 300, 420))
    doc.add_text(DocItemLabel.TEXT, "   ")  # whitespace-only text is dropped
    return doc


def test_mapping() -> None:
    ir = to_ir(sample())
    assert ir["schema_version"] == "1.0"
    assert ir["metadata"] == {"page_count": 2, "title": "Pump Manual", "source_name": "manual"}
    assert ir["pages"] == [{"page": 1, "width": 600.0, "height": 800.0}, {"page": 2, "width": 600.0, "height": 800.0}]
    kinds = [(b["type"], b.get("level"), b["text"]) for b in ir["blocks"]]
    assert kinds[:5] == [
        ("heading", 1, "Pump Manual"),
        ("heading", 2, "Installation"),
        ("paragraph", None, "Mount the pump on a level surface."),
        ("list_item", None, "Close the valve."),
        ("heading", 3, "Specifications"),
    ]
    table = next(b for b in ir["blocks"] if b["type"] == "table")
    assert "Flow" in table["text"] and "40 l/min" in table["text"] and "|" in table["text"]
    assert "<table" in table["html"]
    figure = next(b for b in ir["blocks"] if b["type"] == "figure")
    assert figure["text"] == "Figure 1: exploded view" and figure["page"] == 2
    assert [b["id"] for b in ir["blocks"]] == [f"b{i}" for i in range(1, len(ir["blocks"]) + 1)]
    assert all(b["text"].strip() or b["type"] == "figure" for b in ir["blocks"])


def test_bbox_is_top_left_origin() -> None:
    first = to_ir(sample())["blocks"][0]
    assert first["page"] == 1
    assert first["bbox"] == pytest.approx([50.0, 40.0, 300.0, 60.0])  # 800 - 760 = 40


def test_matches_golden() -> None:
    """``tests/golden/sample_ir.json`` is this sample's IR. RAGCentre's own test suite validates the
    same file (tests/test_m10_docling_shim_golden.py), since the two cannot share an environment.
    Regenerate with ``UPDATE_GOLDEN=1 uv run pytest``."""
    got = to_ir(sample())
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=2) + "\n", encoding="utf-8")
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))


def fake_converter(calls: list[tuple[Any, ...]]) -> Any:
    def convert(content: bytes, filename: str, options: Options) -> DoclingDocument:
        calls.append((content, filename, options.ocr, options.include_page_images))
        if filename.endswith(".xyz"):
            raise RuntimeError(
                "Conversion failed for: m.xyz with status: skipped. Errors: File format not allowed: m.xyz"
            )
        if filename.endswith(".bad"):
            raise RuntimeError("broken file")
        return sample()

    return convert


def test_service_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCLING_SHIM_API_KEY", raising=False)
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_converter(calls)))
    info = client.get("/info").json()
    assert info["protocol"] == PROTOCOL and info["name"] == "docling-shim"
    assert info["version"].startswith("0.2.0+docling.") and ".ocr-" in info["version"]
    assert "application/pdf" in info["mime_types"]
    resp = client.post("/parse", files={"file": ("m.pdf", b"%PDF", "application/pdf")},
                       data={"options": json.dumps({"ocr": False})})  # fmt: skip
    assert resp.status_code == 200 and resp.json()["blocks"][0]["text"] == "Pump Manual"
    assert calls == [(b"%PDF", "m.pdf", False, False)]
    bad = client.post("/parse", files={"file": ("m.pdf", b"x", "application/pdf")}, data={"options": "[1]"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_options"
    unsupported = client.post("/parse", files={"file": ("m.xyz", b"x", "application/octet-stream")})
    assert unsupported.status_code == 415 and unsupported.json()["error"]["code"] == "unsupported_media_type"
    broken = client.post("/parse", files={"file": ("m.bad", b"x", "application/octet-stream")})
    assert broken.status_code == 422 and "broken file" in broken.json()["error"]["message"]


def test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCLING_SHIM_API_KEY", "s3cret")
    client = TestClient(create_app(fake_converter([])))
    # RAGCentre reads /info without credentials when it creates the provider.
    assert client.get("/info").status_code == 200
    assert client.get("/info", headers={"Authorization": "Bearer wrong"}).status_code == 200
    assert client.get("/info/").status_code == 401 and client.post("/info").status_code in (401, 405)
    assert client.post("/parse", files={"file": ("m.pdf", b"x", "application/pdf")}).status_code == 401
    wrong = client.post(
        "/parse", files={"file": ("m.pdf", b"x", "application/pdf")}, headers={"Authorization": "Bearer wrong"}
    )
    assert wrong.status_code == 401
    ok = client.post(
        "/parse", files={"file": ("m.pdf", b"x", "application/pdf")}, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


def post(client: TestClient, options: Any, name: str = "m.pdf") -> Any:
    data = {"options": options if isinstance(options, str) else json.dumps(options)}
    return client.post("/parse", files={"file": (name, b"%PDF", "application/pdf")}, data=data)


def test_unknown_option_is_400_and_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCLING_SHIM_API_KEY", raising=False)
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_converter(calls)))
    resp = post(client, {"ocr": True, "ocrr": False})
    assert resp.status_code == 400
    assert resp.json() == {"error": {"code": "invalid_options", "message": "option 'ocrr' is not recognised"}}
    assert calls == []


@pytest.mark.parametrize("options", [{"ocr": "false"}, {"ocr": 0}, {"ocr": None}, {"include_page_images": "yes"}])
def test_options_are_strictly_typed(monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]) -> None:
    monkeypatch.delenv("DOCLING_SHIM_API_KEY", raising=False)
    calls: list[tuple[Any, ...]] = []
    resp = post(TestClient(create_app(fake_converter(calls))), options)
    key = next(iter(options))
    assert resp.status_code == 400
    assert resp.json()["error"] == {"code": "invalid_options", "message": f"option '{key}' must be true or false"}
    assert calls == []


def test_info_reports_the_options_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCLING_SHIM_API_KEY", raising=False)
    schema = TestClient(create_app(fake_converter([]))).get("/info").json()["options_schema"]
    assert set(schema["properties"]) == {"ocr", "include_page_images"}
    assert schema["additionalProperties"] is False
    assert all(spec["description"] for spec in schema["properties"].values())


# --- the version names the OCR engine --------------------------------------------------------


def fake_environment(monkeypatch: pytest.MonkeyPatch, platform_name: str, installed: set[str]) -> None:
    monkeypatch.setattr(app_module.sys, "platform", platform_name)
    monkeypatch.setattr(app_module, "_has", lambda module: module in installed)
    monkeypatch.setattr(app_module, "version", lambda name: "2.150.1")


@pytest.mark.parametrize(
    ("platform_name", "installed", "engine"),
    [
        ("darwin", {"ocrmac", "onnxruntime", "rapidocr"}, "ocrmac"),
        ("darwin", {"onnxruntime", "rapidocr", "easyocr"}, "rapidocr-onnxruntime"),
        ("linux", {"onnxruntime", "rapidocr", "easyocr", "torch"}, "rapidocr-onnxruntime"),
        ("linux", {"rapidocr", "easyocr", "torch"}, "easyocr"),
        ("linux", {"rapidocr", "torch"}, "rapidocr-torch"),
        ("linux", {"torch"}, "none"),
        ("linux", set(), "none"),
        ("linux", {"ocrmac"}, "none"),  # ocrmac is only tried on macOS
    ],
)
def test_version_names_the_automatic_ocr_engine(
    monkeypatch: pytest.MonkeyPatch, platform_name: str, installed: set[str], engine: str
) -> None:
    fake_environment(monkeypatch, platform_name, installed)
    assert app_module.resolve_ocr_engine() == engine
    assert service_version() == f"0.2.0+docling.2.150.1.ocr-{engine}"


def test_version_ocr_suffix_is_sanitised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "resolve_ocr_engine", lambda: "some engine/2.0")
    monkeypatch.setattr(app_module, "version", lambda name: "2.150.1")
    assert service_version().endswith(".ocr-some-engine-2-0")


# --- the real converter, with a stand-in for the docling package ---------------------------------


class FakeFormatOption:
    def __init__(self, pipeline_options: Any = None) -> None:
        self.pipeline_options = pipeline_options


class FakePipelineOptions:
    def __init__(self) -> None:
        self.do_ocr = True
        self.generate_page_images = False


class FakeDocumentConverter:
    instances: ClassVar[list[FakeDocumentConverter]] = []

    def __init__(self, format_options: dict[str, FakeFormatOption]) -> None:
        self.format_options = format_options
        self.streams: list[Any] = []
        FakeDocumentConverter.instances.append(self)

    def convert(self, stream: Any) -> Any:
        self.streams.append(stream)
        return types.SimpleNamespace(document="doc")


@pytest.fixture
def fake_docling(monkeypatch: pytest.MonkeyPatch) -> type[FakeDocumentConverter]:
    FakeDocumentConverter.instances = []
    base = types.ModuleType("docling.datamodel.base_models")
    base.InputFormat = types.SimpleNamespace(PDF="pdf", IMAGE="image")  # type: ignore[attr-defined]
    base.DocumentStream = lambda name, stream: (name, stream.read())  # type: ignore[attr-defined]
    pipeline = types.ModuleType("docling.datamodel.pipeline_options")
    pipeline.PdfPipelineOptions = FakePipelineOptions  # type: ignore[attr-defined]
    converter = types.ModuleType("docling.document_converter")
    converter.DocumentConverter = FakeDocumentConverter  # type: ignore[attr-defined]
    converter.PdfFormatOption = FakeFormatOption  # type: ignore[attr-defined]
    converter.ImageFormatOption = FakeFormatOption  # type: ignore[attr-defined]
    for name, module in {
        "docling": types.ModuleType("docling"),
        "docling.datamodel": types.ModuleType("docling.datamodel"),
        "docling.datamodel.base_models": base,
        "docling.datamodel.pipeline_options": pipeline,
        "docling.document_converter": converter,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return FakeDocumentConverter


def test_options_reach_the_pdf_and_the_image_pipeline(fake_docling: type[FakeDocumentConverter]) -> None:
    convert = DoclingConverter()
    assert convert(b"png", "scan.png", Options(ocr=False, include_page_images=True)) == "doc"
    (instance,) = fake_docling.instances
    assert instance.streams == [("scan.png", b"png")]
    for kind in ("pdf", "image"):
        pipeline_options = instance.format_options[kind].pipeline_options
        assert pipeline_options.do_ocr is False and pipeline_options.generate_page_images is True


def test_converters_are_cached_per_options(fake_docling: type[FakeDocumentConverter]) -> None:
    convert = DoclingConverter()
    combos = [
        Options(),
        Options(ocr=False),
        Options(include_page_images=True),
        Options(ocr=False, include_page_images=True),
    ]
    for _ in range(2):
        for options in combos:
            convert(b"x", "a.pdf", options)
    assert len(fake_docling.instances) == 4


def test_converter_cache_is_bounded_and_least_recently_used(
    fake_docling: type[FakeDocumentConverter], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MAX_CONVERTERS == 4
    monkeypatch.setattr(app_module, "MAX_CONVERTERS", 2)
    convert = DoclingConverter()
    a, b, c = Options(), Options(ocr=False), Options(include_page_images=True)
    convert(b"x", "a.pdf", a)
    convert(b"x", "a.pdf", b)
    convert(b"x", "a.pdf", a)  # a is now the most recently used
    convert(b"x", "a.pdf", c)  # evicts b
    assert len(fake_docling.instances) == 3
    convert(b"x", "a.pdf", a)  # still cached
    assert len(fake_docling.instances) == 3
    convert(b"x", "a.pdf", b)  # was evicted: built again
    assert len(fake_docling.instances) == 4
