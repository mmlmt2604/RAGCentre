"""Tests for the shim. They need docling-core (the document model) but not Docling's models."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

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
from shimkit import PROTOCOL

from docling_shim.app import create_app
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
    def convert(content: bytes, filename: str, options: dict[str, bool]) -> DoclingDocument:
        calls.append((content, filename, options))
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
    assert info["version"].startswith("0.3.0+docling.")
    assert "application/pdf" in info["mime_types"]
    resp = client.post("/parse", files={"file": ("m.pdf", b"%PDF", "application/pdf")},
                       data={"options": json.dumps({"ocr": False})})  # fmt: skip
    assert resp.status_code == 200 and resp.json()["blocks"][0]["text"] == "Pump Manual"
    assert len(calls) == 1 and calls[0][:2] == (b"%PDF", "m.pdf")
    assert calls[0][2]["ocr"] is False and calls[0][2]["heading_hierarchy"] is True
    assert calls[0][2]["table_mode"] == "accurate" and calls[0][2]["page_range"] is None  # defaults filled in
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
    files = {"file": ("m.pdf", b"%PDF", "application/pdf")}
    assert client.post("/parse", files=files).status_code == 401
    assert client.post("/parse", files=files, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/parse", files=files, headers={"Authorization": "Bearer s3cret"}).status_code == 200


@pytest.mark.parametrize("value", ["false", 0, None, "yes"])
def test_options_are_validated(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    monkeypatch.delenv("DOCLING_SHIM_API_KEY", raising=False)
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_converter(calls)))
    resp = client.post("/parse", files={"file": ("m.pdf", b"%PDF", "application/pdf")},
                       data={"options": json.dumps({"ocr": value})})  # fmt: skip
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_options"
    assert calls == []


def test_heading_hierarchy_option(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCLING_SHIM_API_KEY", raising=False)
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_converter(calls)))
    client.post("/parse", files={"file": ("m.pdf", b"%PDF", "application/pdf")},
                data={"options": json.dumps({"heading_hierarchy": False})})  # fmt: skip
    assert calls[0][2]["heading_hierarchy"] is False


def test_async_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.delenv("DOCLING_SHIM_API_KEY", raising=False)
    client = TestClient(create_app(fake_converter([])))
    assert client.get("/info").json()["capabilities"]["async"] is True
    resp = client.post("/parse?mode=async", files={"file": ("m.pdf", b"%PDF", "application/pdf")})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    for _ in range(100):
        if client.get(f"/jobs/{job_id}").json()["status"] == "succeeded":
            break
        time.sleep(0.02)
    assert client.get(f"/jobs/{job_id}/result").json()["blocks"][0]["text"] == "Pump Manual"


# --- remote mode: an existing docling-serve ------------------------------------------------------


def serve_response() -> dict[str, Any]:
    """What docling-serve's ``POST /v1/convert/file`` returns for ``to_formats=json``."""
    return {
        "document": {"filename": "m.pdf", "json_content": sample().model_dump(mode="json", by_alias=True)},
        "status": "success",
        "errors": [],
    }


def remote_client(handler: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import httpx

    from docling_shim.remote import DoclingServeConverter

    monkeypatch.delenv("DOCLING_SHIM_API_KEY", raising=False)
    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"X-Api-Key": "serve-key"})
    return TestClient(create_app(DoclingServeConverter("http://serve:5001/", client=http)))


def test_remote_matches_the_in_process_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/version":
            return httpx.Response(200, json={"docling": "2.150.1", "docling-serve": "1.9.0"})
        return httpx.Response(200, json=serve_response())

    client = remote_client(handler, monkeypatch)
    resp = client.post("/parse", files={"file": ("m.pdf", b"%PDF", "application/pdf")},
                       data={"options": json.dumps({"ocr": False, "heading_hierarchy": True})})  # fmt: skip
    assert resp.status_code == 200
    assert resp.json() == json.loads(GOLDEN.read_text(encoding="utf-8"))
    request = seen[0]
    assert request.url.path == "/v1/convert/file" and request.headers["x-api-key"] == "serve-key"
    body = request.content.decode("latin-1")
    for expected in ('name="files"', 'name="to_formats"\r\n\r\njson', 'name="do_ocr"\r\n\r\nfalse',
                     'name="do_pdf_heading_hierarchy"\r\n\r\ntrue'):  # fmt: skip
        assert expected in body, expected
    assert client.get("/info").json()["version"] == "0.3.0+docling-serve.2.150.1"


def test_remote_version_without_a_version_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    client = remote_client(lambda request: httpx.Response(404), monkeypatch)
    assert client.get("/info").json()["version"] == "0.3.0+docling-serve.remote"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(415, 415), (422, 422), (401, 502), (500, 503), (429, 503)],
)
def test_remote_errors_are_classified(monkeypatch: pytest.MonkeyPatch, status: int, expected: int) -> None:
    import httpx

    client = remote_client(lambda request: httpx.Response(status, json={"detail": "nope"}), monkeypatch)
    resp = client.post("/parse", files={"file": ("m.pdf", b"%PDF", "application/pdf")})
    assert resp.status_code == expected


def test_remote_unreachable_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = remote_client(handler, monkeypatch)
    resp = client.post("/parse", files={"file": ("m.pdf", b"%PDF", "application/pdf")})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "upstream_unreachable"


def test_remote_failed_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    body = {"document": {"filename": "m.pdf"}, "status": "failure", "errors": [{"error_message": "bad pdf"}]}
    client = remote_client(lambda request: httpx.Response(200, json=body), monkeypatch)
    resp = client.post("/parse", files={"file": ("m.pdf", b"%PDF", "application/pdf")})
    assert resp.status_code == 422 and "bad pdf" in resp.json()["error"]["message"]


def test_remote_mode_is_chosen_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from docling_shim.remote import DoclingServeConverter

    monkeypatch.delenv("DOCLING_SERVE_URL", raising=False)
    assert DoclingServeConverter.from_env() is None
    monkeypatch.setenv("DOCLING_SERVE_URL", "http://serve:5001/")
    monkeypatch.setenv("DOCLING_SERVE_API_KEY", "k")
    converter = DoclingServeConverter.from_env()
    assert converter is not None and converter.url == "http://serve:5001"
    assert converter.client.headers["x-api-key"] == "k"


# --- engine options -------------------------------------------------------------------------------


def all_options(**changes: Any) -> dict[str, Any]:
    from shimkit import parse_options

    from docling_shim.app import OPTIONS

    return parse_options(OPTIONS, changes)


def pipeline_of(converter: Any) -> Any:
    from docling.datamodel.base_models import InputFormat

    return converter.format_to_options[InputFormat.PDF].pipeline_options


def test_options_reach_the_pipeline() -> None:
    from docling.datamodel import pipeline_options as po

    from docling_shim.app import DoclingConverter

    converter = DoclingConverter()
    pipeline = pipeline_of(
        converter._converter(
            all_options(
                ocr_engine="tesseract",
                ocr_lang=["eng", "deu"],
                force_full_page_ocr=True,
                table_mode="fast",
                code_enrichment=True,
                formula_enrichment=True,
                picture_classification=True,
                picture_description=True,
                heading_hierarchy=False,
                include_page_images=True,
            )
        )
    )
    assert isinstance(pipeline.ocr_options, po.TesseractCliOcrOptions)
    assert pipeline.ocr_options.lang == ["eng", "deu"] and pipeline.ocr_options.mode == po.OcrMode.FULL_PAGE
    assert pipeline.table_structure_options.mode == po.TableFormerMode.FAST
    assert pipeline.do_code_enrichment and pipeline.do_formula_enrichment
    assert pipeline.do_picture_classification and pipeline.do_picture_description
    assert not pipeline.heading_hierarchy_options.enabled and pipeline.generate_page_images
    default = pipeline_of(converter._converter(all_options()))
    assert isinstance(default.ocr_options, po.OcrAutoOptions) and default.ocr_options.mode == po.OcrMode.DEFAULT
    assert default.table_structure_options.mode == po.TableFormerMode.ACCURATE
    assert default.heading_hierarchy_options.enabled and not default.do_code_enrichment


@pytest.mark.parametrize(
    ("engine", "class_name"),
    [
        ("auto", "OcrAutoOptions"),
        ("easyocr", "EasyOcrOptions"),
        ("rapidocr", "RapidOcrOptions"),
        ("ocrmac", "OcrMacOptions"),
    ],
)
def test_every_ocr_engine_builds(engine: str, class_name: str) -> None:
    from docling_shim.app import DoclingConverter

    pipeline = pipeline_of(DoclingConverter()._converter(all_options(ocr_engine=engine)))
    assert type(pipeline.ocr_options).__name__ == class_name


def test_converters_are_cached_per_pipeline_options_but_not_per_page_window() -> None:
    from docling_shim.app import DoclingConverter

    converter = DoclingConverter()
    first = converter._converter(all_options())
    assert converter._converter(all_options(page_range=[2, 3], max_pages=1)) is first
    assert converter._converter(all_options(ocr_lang=["en"])) is not first
    assert converter._converter(all_options(ocr_lang=["en"])) is converter._converter(all_options(ocr_lang=["en"]))
    assert len(converter._converters) == 2


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, {}),
        ({"page_range": [2, 4]}, {"page_range": (2, 4)}),
        ({"max_pages": 3}, {"page_range": (1, 3)}),
        ({"page_range": [5, 50], "max_pages": 10}, {"page_range": (5, 14)}),
        ({"page_range": [5, 8], "max_pages": 10}, {"page_range": (5, 8)}),
    ],
)
def test_page_window(changes: dict[str, Any], expected: dict[str, Any]) -> None:
    from docling_shim.app import _page_arguments

    assert _page_arguments(all_options(**changes)) == expected


@pytest.mark.parametrize(
    "bad",
    [{"ocr_engine": "paddle"}, {"table_mode": "slow"}, {"page_range": [3, 1]}, {"max_pages": 0}, {"ocr_lang": "en"}],
)
def test_bad_engine_options_are_a_400(bad: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCLING_SHIM_API_KEY", raising=False)
    client = TestClient(create_app(fake_converter([])))
    resp = client.post(
        "/parse", files={"file": ("m.pdf", b"%PDF", "application/pdf")}, data={"options": json.dumps(bad)}
    )
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_options"


def test_remote_form_fields() -> None:
    from docling_shim.remote import form_fields

    assert form_fields(all_options()) == {
        "to_formats": "json", "image_export_mode": "placeholder", "do_ocr": "true", "include_page_images": "false",
        "do_pdf_heading_hierarchy": "true", "ocr_engine": "auto", "force_ocr": "false", "table_mode": "accurate",
        "do_code_enrichment": "false", "do_formula_enrichment": "false", "do_picture_classification": "false",
        "do_picture_description": "false",
    }  # fmt: skip
    got = form_fields(all_options(ocr_lang=["en", "de"], page_range=[2, 9], max_pages=3, table_mode="fast"))
    assert got["ocr_lang"] == ["en", "de"] and got["page_range"] == ["2", "4"] and got["table_mode"] == "fast"
    assert form_fields(all_options(max_pages=5))["page_range"] == ["1", "5"]


def test_remote_sends_a_repeated_field_for_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=serve_response())

    client = remote_client(handler, monkeypatch)
    client.post("/parse", files={"file": ("m.pdf", b"%PDF", "application/pdf")},
                data={"options": json.dumps({"ocr_lang": ["en", "de"], "page_range": [2, 4]})})  # fmt: skip
    body = seen[0].content.decode("latin-1")
    assert body.count('name="ocr_lang"') == 2 and body.count('name="page_range"') == 2


# --- richer mapping: pictures, code and items that run over a page break ---------------------------


def rich_sample() -> DoclingDocument:
    from docling_core.types.doc import CodeLanguageLabel
    from docling_core.types.doc.document import (
        DescriptionMetaField,
        PictureClassificationMetaField,
        PictureClassificationPrediction,
        PictureMeta,
    )

    doc = DoclingDocument(name="rich")
    doc.add_page(page_no=1, size=Size(width=600, height=800))
    doc.add_page(page_no=2, size=Size(width=600, height=800))
    caption = doc.add_text(DocItemLabel.CAPTION, "Figure 2: throughput", prov=prov(1, 50, 300, 300, 280))
    picture = doc.add_picture(caption=caption, prov=prov(1, 50, 500, 300, 320))
    picture.meta = PictureMeta(
        description=DescriptionMetaField(text="A bar chart of litres per minute.", created_by="test"),
        classification=PictureClassificationMetaField(
            predictions=[
                PictureClassificationPrediction(class_name="photograph", confidence=0.2, created_by="test"),
                PictureClassificationPrediction(class_name="bar_chart", confidence=0.9, created_by="test"),
            ]
        ),
    )
    bare = doc.add_picture(prov=prov(1, 50, 250, 300, 150))  # no caption, no description
    bare.meta = PictureMeta(description=DescriptionMetaField(text="A company logo.", created_by="test"))
    doc.add_code("print('hi')", code_language=CodeLanguageLabel.PYTHON, prov=prov(1, 50, 100, 300, 80))
    doc.add_code("echo hi", prov=prov(1, 50, 70, 300, 60))  # language unknown
    cells = [
        TableCell(text="Flow", start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=0,
                  end_col_offset_idx=1, column_header=True),
        TableCell(text="40 l/min", start_row_offset_idx=1, end_row_offset_idx=2, start_col_offset_idx=0,
                  end_col_offset_idx=1),
    ]  # fmt: skip
    table = doc.add_table(data=TableData(num_rows=2, num_cols=1, table_cells=cells), prov=prov(1, 50, 50, 300, 10))
    table.prov.append(prov(2, 50, 760, 300, 700))  # the table continues on page 2
    return doc


def test_picture_description_class_code_language_and_page_span() -> None:
    blocks = {b["text"][:12]: b for b in to_ir(rich_sample())["blocks"]}
    figure = blocks["Figure 2: th"]
    assert figure["type"] == "figure" and figure["text"] == "Figure 2: throughput\n\nA bar chart of litres per minute."
    assert figure["metadata"] == {"classification": "bar_chart"}  # the most confident class
    assert blocks["A company lo"]["text"] == "A company logo." and "metadata" not in blocks["A company lo"]
    assert blocks["print('hi')"]["metadata"] == {"language": "Python"}
    assert "metadata" not in blocks["echo hi"]
    table = next(b for b in to_ir(rich_sample())["blocks"] if b["type"] == "table")
    assert table["page"] == 1 and table["metadata"] == {"page_end": 2}
    single = next(b for b in to_ir(sample())["blocks"] if b["type"] == "table")
    assert "metadata" not in single  # one page: no page_end


def test_legacy_picture_annotations_are_still_read() -> None:
    from docling_core.types.doc.document import (
        PictureClassificationClass,
        PictureClassificationData,
        PictureDescriptionData,
    )

    doc = rich_sample()
    old = doc.add_picture(prov=prov(2, 50, 600, 300, 500))
    old.annotations = [
        PictureDescriptionData(text="An old-style description.", provenance="x"),
        PictureClassificationData(
            provenance="x", predicted_classes=[PictureClassificationClass(class_name="logo", confidence=0.8)]
        ),
    ]
    figure = next(b for b in to_ir(doc)["blocks"] if b["text"] == "An old-style description.")
    assert figure["metadata"] == {"classification": "logo"}


def test_remote_document_keeps_the_new_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    body = {"document": {"filename": "r.pdf", "json_content": rich_sample().model_dump(mode="json", by_alias=True)},
            "status": "success", "errors": []}  # fmt: skip
    client = remote_client(lambda request: httpx.Response(200, json=body), monkeypatch)
    ir = client.post("/parse", files={"file": ("r.pdf", b"%PDF", "application/pdf")}).json()
    assert ir == to_ir(rich_sample()) | {"metadata": ir["metadata"]}
    assert any(b.get("metadata", {}).get("page_end") == 2 for b in ir["blocks"])
