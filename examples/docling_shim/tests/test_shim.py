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
from docling_core.types.doc.document import (
    DescriptionMetaField,
    PictureClassificationMetaField,
    PictureClassificationPrediction,
    PictureMeta,
)
from fake_docling import FakeDocumentConverter
from fastapi.testclient import TestClient

from docling_shim import app as app_module
from docling_shim import engine as engine_module
from docling_shim.app import MAX_CONVERTERS, PROTOCOL, DoclingConverter, Options, create_app, service_version
from docling_shim.convert import to_ir
from docling_shim.engine import Settings, ShimError, build_pipeline_options
from docling_shim.options import OptionsError, parse_options

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
    picture = doc.add_picture(caption=caption, prov=prov(2, 50, 580, 300, 420))
    picture.meta = PictureMeta(
        description=DescriptionMetaField(text="An exploded view of the pump.", created_by="vlm"),
        classification=PictureClassificationMetaField(
            predictions=[
                PictureClassificationPrediction(class_name="photograph", confidence=0.1, created_by="m"),
                PictureClassificationPrediction(class_name="engineering_drawing", confidence=0.8, created_by="m"),
            ]
        ),
    )
    runs_on = doc.add_text(
        DocItemLabel.TEXT, "A paragraph that runs over the page break.", prov=prov(2, 50, 360, 550, 340)
    )
    runs_on.prov.append(prov(2, 50, 330, 550, 300))
    over = doc.add_text(DocItemLabel.TEXT, "Continued on the next page.", prov=prov(1, 50, 90, 550, 70))
    over.prov.append(prov(2, 50, 790, 550, 770))
    doc.add_text(DocItemLabel.TEXT, "   ")  # whitespace-only text is dropped
    return doc


def test_mapping() -> None:
    ir = to_ir(sample())
    assert ir["schema_version"] == "1.1"
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
    assert figure["text"] == "Figure 1: exploded view\n\nAn exploded view of the pump." and figure["page"] == 2
    assert figure["metadata"] == {
        "description": "An exploded view of the pump.",
        "classification": "engineering_drawing",
    }
    assert [b["id"] for b in ir["blocks"]] == [f"b{i}" for i in range(1, len(ir["blocks"]) + 1)]
    assert all(b["text"].strip() or b["type"] == "figure" for b in ir["blocks"])


def test_an_item_over_a_page_break_has_spans() -> None:
    blocks = {b["text"]: b for b in to_ir(sample())["blocks"]}
    over = blocks["Continued on the next page."]
    assert over["page"] == 1 and over["bbox"] == pytest.approx([50.0, 710.0, 550.0, 730.0])
    assert [s["page"] for s in over["spans"]] == [1, 2]
    assert over["spans"][0]["bbox"] == pytest.approx(over["bbox"])  # the first span is page and bbox
    assert over["spans"][1]["bbox"] == pytest.approx([50.0, 10.0, 550.0, 30.0])
    same_page = blocks["A paragraph that runs over the page break."]
    assert [s["page"] for s in same_page["spans"]] == [2, 2]
    assert "spans" not in blocks["Mount the pump on a level surface."]  # one provenance entry: no spans


def test_figure_text_is_the_caption_then_the_description() -> None:
    from docling_shim.convert import to_ir as convert

    doc = DoclingDocument(name="m")
    doc.add_page(page_no=1, size=Size(width=600, height=800))
    doc.add_picture(prov=prov(1, 0, 100, 100, 0))  # no caption, nothing described
    only_description = doc.add_picture(prov=prov(1, 0, 200, 100, 100))
    only_description.meta = PictureMeta(description=DescriptionMetaField(text="A logo.", created_by="vlm"))
    blocks = convert(doc)["blocks"]
    assert blocks[0]["text"] == "" and "metadata" not in blocks[0]
    assert blocks[1]["text"] == "A logo." and blocks[1]["metadata"] == {"description": "A logo."}


def test_older_annotations_are_read_when_there_is_no_meta() -> None:
    from docling_core.types.doc.document import (
        PictureClassificationClass,
        PictureClassificationData,
        PictureDescriptionData,
    )

    doc = DoclingDocument(name="m")
    doc.add_page(page_no=1, size=Size(width=600, height=800))
    picture = doc.add_picture(prov=prov(1, 0, 100, 100, 0))
    picture.annotations.append(
        PictureClassificationData(
            provenance="m",
            predicted_classes=[
                PictureClassificationClass(class_name="logo", confidence=0.2),
                PictureClassificationClass(class_name="bar_chart", confidence=0.7),
            ],
        )
    )
    picture.annotations.append(PictureDescriptionData(provenance="m", text="Sales by quarter."))
    (block,) = to_ir(doc)["blocks"]
    assert block["text"] == "Sales by quarter."
    assert block["metadata"] == {"description": "Sales by quarter.", "classification": "bar_chart"}


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
    assert info["version"].startswith("0.3.0+docling.") and ".ocr-" in info["version"]
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
    assert service_version() == f"0.3.0+docling.2.150.1.ocr-{engine}"


def test_version_ocr_suffix_is_sanitised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "resolve_ocr_engine", lambda: "some engine/2.0")
    monkeypatch.setattr(app_module, "version", lambda name: "2.150.1")
    assert service_version().endswith(".ocr-some-engine-2-0")


# --- options ---------------------------------------------------------------------------------------


def opts(**kwargs: Any) -> Options:
    return parse_options(kwargs)


def test_info_schema_lists_every_named_option(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCLING_SHIM_API_KEY", raising=False)
    schema = TestClient(create_app(fake_converter([]))).get("/info").json()["options_schema"]
    assert set(schema["properties"]) == {
        "pipeline", "vlm_model", "ocr", "include_page_images", "page_range", "languages", "tables", "table_mode", "ocr_engine",
        "force_full_page_ocr", "formulas", "code", "picture_classification", "picture_description", "charts",
        "images_scale", "engine",
    }  # fmt: skip
    assert schema["properties"]["engine"]["type"] == "object"
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"table_mode": "slow"}, "option 'table_mode' must be fast or accurate"),
        ({"ocr_engine": "nope"}, "option 'ocr_engine' must be auto, easyocr, rapidocr, tesseract or tesseract_cli"),
        ({"images_scale": 0}, "option 'images_scale' must be a number that is greater than 0 and at most 4"),
        ({"images_scale": 9}, "option 'images_scale' must be a number that is greater than 0 and at most 4"),
        ({"languages": "en"}, "option 'languages' must be a list"),
        ({"languages": [1]}, "option 'languages' is invalid: Input should be a valid string"),
        ({"page_range": [3, 1]}, "option 'page_range' is invalid: must be [first, last] with first not after last"),
        ({"page_range": [0, 1]}, "option 'page_range' is invalid: Input should be greater than or equal to 1"),
        (
            {"page_range": [1]},
            "option 'page_range' is invalid: List should have at least 2 items after validation, not 1",
        ),
        ({"engine": []}, "option 'engine' must be an object"),
        ({"formulas": "yes"}, "option 'formulas' must be true or false"),
    ],
)
def test_option_errors_name_the_option(options: dict[str, Any], message: str) -> None:
    with pytest.raises(OptionsError) as err:
        parse_options(options)
    assert str(err.value) == message


def test_named_options_reach_the_pipeline(fake_docling: Any) -> None:
    pipeline = build_pipeline_options(
        opts(
            ocr=False, include_page_images=True, tables=False, table_mode="fast", formulas=True, code=True,
            picture_classification=True, picture_description=True, charts=True, images_scale=2,
        )
    )  # fmt: skip
    assert pipeline.do_ocr is False and pipeline.generate_page_images is True
    assert pipeline.do_table_structure is False and pipeline.table_structure_options.mode.value == "fast"
    assert pipeline.do_formula_enrichment and pipeline.do_code_enrichment and pipeline.do_chart_extraction
    assert pipeline.do_picture_classification and pipeline.do_picture_description
    assert pipeline.images_scale == 2.0


def test_defaults_match_doclings_own(fake_docling: Any) -> None:
    from fake_docling import PdfPipelineOptions

    built, reference = build_pipeline_options(opts()), PdfPipelineOptions()
    assert built.model_dump() == reference.model_dump()


@pytest.mark.parametrize(
    ("engine", "cls", "kind"),
    [
        ("auto", "OcrAutoOptions", "auto"),
        ("easyocr", "EasyOcrOptions", "easyocr"),
        ("rapidocr", "RapidOcrOptions", "rapidocr"),
        ("tesseract", "TesseractOcrOptions", "tesserocr"),
        ("tesseract_cli", "TesseractCliOcrOptions", "tesseract"),
    ],
)
def test_ocr_engine_picks_the_options_class(
    fake_docling: Any, monkeypatch: pytest.MonkeyPatch, engine: str, cls: str, kind: str
) -> None:
    monkeypatch.setattr(engine_module, "_installed", lambda module: True)
    monkeypatch.setattr(engine_module.shutil, "which", lambda name: "/usr/bin/tesseract")
    pipeline = build_pipeline_options(opts(ocr_engine=engine, languages=["x"], force_full_page_ocr=True))
    assert type(pipeline.ocr_options).__name__ == cls and pipeline.ocr_options.kind == kind
    assert pipeline.ocr_options.lang == ["x"] and pipeline.ocr_options.mode.value == "full_page"


def test_languages_default_to_the_engines_own(fake_docling: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "_installed", lambda module: True)
    assert build_pipeline_options(opts(ocr_engine="easyocr")).ocr_options.lang == ["en", "es", "fr", "de"]
    assert build_pipeline_options(opts(ocr_engine="easyocr")).ocr_options.mode.value == "default"


@pytest.mark.parametrize(
    ("engine", "missing"),
    [("easyocr", "easyocr"), ("rapidocr", "rapidocr"), ("tesseract", "tesserocr")],
)
def test_a_missing_ocr_package_is_503_and_named(
    fake_docling: Any, monkeypatch: pytest.MonkeyPatch, engine: str, missing: str
) -> None:
    monkeypatch.setattr(engine_module, "_installed", lambda module: False)
    with pytest.raises(ShimError) as err:
        build_pipeline_options(opts(ocr_engine=engine))
    assert err.value.status == 503 and err.value.code == "engine_unavailable" and f"'{missing}'" in err.value.message


def test_missing_tesseract_program_is_503(fake_docling: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module.shutil, "which", lambda name: None)
    with pytest.raises(ShimError) as err:
        build_pipeline_options(opts(ocr_engine="tesseract_cli"))
    assert err.value.status == 503 and "tesseract" in err.value.message


def test_ocr_off_does_not_need_the_ocr_package(fake_docling: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "_installed", lambda module: False)
    assert build_pipeline_options(opts(ocr=False, ocr_engine="easyocr")).do_ocr is False


# --- the engine passthrough ---------------------------------------------------------------------


def test_engine_is_merged_over_the_named_options(fake_docling: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "_installed", lambda module: True)
    pipeline = build_pipeline_options(
        opts(
            ocr_engine="easyocr", tables=False,
            engine={"table_structure_options": {"do_cell_matching": False}, "ocr_options": {"confidence_threshold": 0.8}},
        )
    )  # fmt: skip
    assert pipeline.table_structure_options.do_cell_matching is False
    assert pipeline.table_structure_options.mode.value == "accurate"  # siblings survive the merge
    assert type(pipeline.ocr_options).__name__ == "EasyOcrOptions"  # the class survives the merge
    assert pipeline.ocr_options.confidence_threshold == 0.8 and pipeline.do_table_structure is False


def test_engine_sets_a_field_no_named_option_covers(fake_docling: Any) -> None:
    assert build_pipeline_options(opts(engine={"do_picture_classification": True})).do_picture_classification is True
    # A nested dict is merged, not replaced.
    pipeline = build_pipeline_options(opts(ocr_engine="auto", engine={"ocr_options": {"scale": 2.0}}))
    assert pipeline.ocr_options.scale == 2.0


@pytest.mark.parametrize(
    ("engine", "message"),
    [
        ({"nonsense": 1}, "option 'engine' has no field 'nonsense'"),
        ({"ocr_options": {"nonsense": 1}}, "option 'engine' has no field 'ocr_options.nonsense'"),
        ({"images_scale": "big"}, "option 'engine' is invalid at 'images_scale'"),
        ({"table_structure_options": {"mode": "slow"}}, "option 'engine' is invalid at 'table_structure_options.mode'"),
    ],
)
def test_a_bad_engine_value_is_an_option_error(fake_docling: Any, engine: dict[str, Any], message: str) -> None:
    with pytest.raises(OptionsError) as err:
        build_pipeline_options(opts(engine=engine))
    assert str(err.value).startswith(message)


@pytest.mark.parametrize(
    ("engine", "key"),
    [
        ({"artifacts_path": "/models"}, "artifacts_path"),
        ({"enable_remote_services": True}, "enable_remote_services"),
        ({"allow_external_plugins": True}, "allow_external_plugins"),
        ({"accelerator_options": {"device": "cuda"}}, "accelerator_options"),
        ({"document_timeout": 5}, "document_timeout"),
        ({"ocr_batch_size": 16}, "ocr_batch_size"),
        ({"queue_max_size": 5}, "queue_max_size"),
        ({"batch_polling_interval_seconds": 1}, "batch_polling_interval_seconds"),
        ({"stage_shutdown_timeout_seconds": 1}, "stage_shutdown_timeout_seconds"),
        # depth two and beyond
        ({"picture_description_options": {"url": "http://evil"}}, "picture_description_options.url"),
        ({"picture_description_options": {"headers": {"x": "y"}}}, "picture_description_options.headers"),
        (
            {"ocr_options": {"rapidocr_params": {"Det": {"model_path": "/x"}}}},
            "ocr_options.rapidocr_params.Det.model_path",
        ),
        ({"ocr_options": {"tesseract_cmd": "sh"}}, "ocr_options.tesseract_cmd"),
        ({"ocr_options": {"path": "/tessdata"}}, "ocr_options.path"),
        ({"x": [{"api_key": "k"}]}, "x.api_key"),
    ],
)
def test_denied_engine_keys_are_refused_at_any_depth(engine: dict[str, Any], key: str) -> None:
    with pytest.raises(OptionsError) as err:
        parse_options({"engine": engine})
    assert f"'{key}'" in str(err.value) and "service setting" in str(err.value)


@pytest.mark.parametrize(
    ("named", "engine"),
    [
        ({"ocr": True}, {"do_ocr": False}),
        ({"include_page_images": False}, {"generate_page_images": True}),
        ({"tables": True}, {"do_table_structure": False}),
        ({"table_mode": "fast"}, {"table_structure_options": {"mode": "accurate"}}),
        ({"languages": ["en"]}, {"ocr_options": {"lang": ["fr"]}}),
        ({"force_full_page_ocr": True}, {"ocr_options": {"mode": "default"}}),
        ({"formulas": True}, {"do_formula_enrichment": False}),
        ({"code": True}, {"do_code_enrichment": False}),
        ({"picture_classification": True}, {"do_picture_classification": False}),
        ({"picture_description": True}, {"do_picture_description": False}),
        ({"charts": True}, {"do_chart_extraction": False}),
        ({"images_scale": 2}, {"images_scale": 3}),
    ],
)
def test_a_named_option_and_the_same_setting_in_engine_is_refused(
    named: dict[str, Any], engine: dict[str, Any]
) -> None:
    with pytest.raises(OptionsError) as err:
        parse_options({**named, "engine": engine})
    name = next(iter(named))
    assert f"option {name!r} already sets" in str(err.value)
    # Without the named option the passthrough may set it.
    parse_options({"engine": engine})


def test_the_service_answers_denied_and_bad_engine_options_with_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCLING_SHIM_API_KEY", raising=False)
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_converter(calls)))
    resp = post(client, {"engine": {"ocr_options": {"url": "http://x"}}})
    assert resp.status_code == 400 and "'ocr_options.url'" in resp.json()["error"]["message"]
    assert calls == []

    def raising(content: bytes, filename: str, options: Options) -> Any:
        raise OptionsError("option 'engine' has no field 'zzz'")

    resp = post(TestClient(create_app(raising)), {"engine": {"zzz": 1}})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_options"

    def unavailable(content: bytes, filename: str, options: Options) -> Any:
        raise ShimError(503, "engine_unavailable", "ocr_engine 'easyocr' needs the Python package 'easyocr'")

    resp = post(TestClient(create_app(unavailable)), {"ocr_engine": "easyocr"})
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "engine_unavailable"


# --- service settings ----------------------------------------------------------------------------


def test_picture_description_uses_the_configured_server(fake_docling: Any) -> None:
    settings = Settings(
        picture_description_url="http://llm:8000/v1/chat/completions",
        picture_description_model="llava",
        picture_description_api_key="k",
    )
    pipeline = build_pipeline_options(opts(picture_description=True), settings)
    described = pipeline.picture_description_options
    assert described.url == "http://llm:8000/v1/chat/completions"
    assert described.params == {"model": "llava"} and described.headers == {"Authorization": "Bearer k"}
    assert pipeline.enable_remote_services is True
    # Not asked for: no remote services.
    off = build_pipeline_options(opts(), settings)
    assert off.enable_remote_services is False and off.do_picture_description is False


def test_picture_description_without_a_server_uses_the_local_model(fake_docling: Any) -> None:
    pipeline = build_pipeline_options(opts(picture_description=True), Settings())
    assert type(pipeline.picture_description_options).__name__ == "PictureDescriptionVlmOptions"
    assert pipeline.enable_remote_services is False


def test_device_and_threads_are_service_settings(fake_docling: Any) -> None:
    pipeline = build_pipeline_options(opts(), Settings(device="cpu", num_threads=2))
    assert pipeline.accelerator_options.device == "cpu" and pipeline.accelerator_options.num_threads == 2
    assert build_pipeline_options(opts(), Settings()).accelerator_options.device == "auto"


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCLING_SHIM_DEVICE", " cuda ")
    monkeypatch.setenv("DOCLING_SHIM_NUM_THREADS", "8")
    monkeypatch.setenv("DOCLING_SHIM_PICTURE_DESCRIPTION_URL", "http://x")
    settings = Settings.from_env()
    assert settings.device == "cuda" and settings.num_threads == 8 and settings.picture_description_url == "http://x"
    monkeypatch.setenv("DOCLING_SHIM_NUM_THREADS", "many")
    assert Settings.from_env().num_threads is None


def test_the_description_model_is_part_of_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_environment(monkeypatch, "linux", {"easyocr"})
    monkeypatch.delenv("DOCLING_SHIM_PICTURE_DESCRIPTION_MODEL", raising=False)
    assert service_version() == "0.3.0+docling.2.150.1.ocr-easyocr"
    monkeypatch.setenv("DOCLING_SHIM_PICTURE_DESCRIPTION_MODEL", "org/llava:7b")
    assert service_version() == "0.3.0+docling.2.150.1.ocr-easyocr.desc-org-llava-7b"
    # URL, key, device and threads are not.
    monkeypatch.delenv("DOCLING_SHIM_PICTURE_DESCRIPTION_MODEL")
    for name in ("PICTURE_DESCRIPTION_URL", "PICTURE_DESCRIPTION_API_KEY", "DEVICE", "NUM_THREADS"):
        monkeypatch.setenv(f"DOCLING_SHIM_{name}", "3")
    assert service_version() == "0.3.0+docling.2.150.1.ocr-easyocr"


# --- the converter -------------------------------------------------------------------------------


def test_options_reach_the_pdf_and_the_image_pipeline(fake_docling: type[FakeDocumentConverter]) -> None:
    convert = DoclingConverter(Settings())
    assert convert(b"png", "scan.png", opts(ocr=False, include_page_images=True)) == "doc"
    (instance,) = fake_docling.instances
    assert instance.calls[0][0] == ("scan.png", b"png")
    for kind in ("pdf", "image"):
        pipeline_options = instance.format_options[kind].pipeline_options
        assert pipeline_options.do_ocr is False and pipeline_options.generate_page_images is True


def test_page_range_reaches_convert_as_a_tuple_and_does_not_make_a_new_converter(
    fake_docling: type[FakeDocumentConverter],
) -> None:
    convert = DoclingConverter(Settings())
    convert(b"x", "a.pdf", opts(page_range=[2, 4]))
    convert(b"x", "a.pdf", opts(page_range=[5, 6]))
    convert(b"x", "a.pdf", opts())
    (instance,) = fake_docling.instances
    assert [kwargs for _, kwargs in instance.calls] == [{"page_range": (2, 4)}, {"page_range": (5, 6)}, {}]


def test_converters_are_cached_per_options(fake_docling: type[FakeDocumentConverter]) -> None:
    convert = DoclingConverter(Settings())
    combos = [opts(), opts(ocr=False), opts(tables=False), opts(engine={"do_picture_classification": True})]
    for _ in range(2):
        for options in combos:
            convert(b"x", "a.pdf", options)
    assert len(fake_docling.instances) == 4


def test_converter_cache_is_bounded_and_least_recently_used(
    fake_docling: type[FakeDocumentConverter], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MAX_CONVERTERS == 4
    monkeypatch.setattr(engine_module, "MAX_CONVERTERS", 2)
    convert = DoclingConverter(Settings())
    a, b, c = opts(), opts(ocr=False), opts(tables=False)
    convert(b"x", "a.pdf", a)
    convert(b"x", "a.pdf", b)
    convert(b"x", "a.pdf", a)  # a is now the most recently used
    convert(b"x", "a.pdf", c)  # evicts b
    assert len(fake_docling.instances) == 3
    convert(b"x", "a.pdf", a)  # still cached
    assert len(fake_docling.instances) == 3
    convert(b"x", "a.pdf", b)  # was evicted: built again
    assert len(fake_docling.instances) == 4


def test_rapidocr_needs_the_package_of_its_backend(fake_docling: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "_installed", lambda module: module in {"rapidocr", "torch"})
    with pytest.raises(ShimError) as err:
        build_pipeline_options(opts(ocr_engine="rapidocr"))  # the default backend is onnxruntime
    assert "'onnxruntime'" in err.value.message
    pipeline = build_pipeline_options(opts(ocr_engine="rapidocr", engine={"ocr_options": {"backend": "torch"}}))
    assert pipeline.ocr_options.backend == "torch"


# --- the vlm pipeline ----------------------------------------------------------------------------


def test_the_vlm_pipeline_builds_vlm_options_with_the_chosen_preset(fake_docling: Any) -> None:
    pipeline = build_pipeline_options(
        opts(pipeline="vlm", vlm_model="smoldocling", include_page_images=False, images_scale=2, charts=True)
    )
    assert type(pipeline).__name__ == "VlmPipelineOptions" and pipeline.vlm_options.preset == "smoldocling"
    assert pipeline.generate_page_images is False and pipeline.images_scale == 2.0 and pipeline.do_chart_extraction
    assert build_pipeline_options(opts(pipeline="vlm")).vlm_options.preset == "granite_docling"


def test_the_vlm_pipeline_runs_pdfs_and_images_through_vlm_pipeline(fake_docling: Any) -> None:
    convert = DoclingConverter(Settings())
    convert(b"x", "a.pdf", opts(pipeline="vlm"))
    (instance,) = fake_docling.instances
    for kind in ("pdf", "image"):
        assert instance.format_options[kind].pipeline_cls == "VlmPipeline"
    convert(b"x", "a.pdf", opts())
    assert fake_docling.instances[1].format_options["pdf"].pipeline_cls is None  # the standard pipeline


@pytest.mark.parametrize(
    "name", ["ocr", "tables", "table_mode", "ocr_engine", "languages", "force_full_page_ocr", "formulas", "code"]
)
def test_options_of_the_standard_pipeline_are_refused_with_vlm(name: str) -> None:
    value: Any = {"languages": ["en"], "table_mode": "fast", "ocr_engine": "auto"}.get(name, False)
    with pytest.raises(OptionsError, match=f"option '{name}' does not apply to the vlm pipeline"):
        parse_options({"pipeline": "vlm", name: value})
    parse_options({"pipeline": "standard", name: value})  # fine there


def test_shared_options_work_with_vlm_and_vlm_model_needs_the_vlm_pipeline() -> None:
    parse_options({"pipeline": "vlm", "include_page_images": True, "images_scale": 2, "page_range": [1, 2]})
    with pytest.raises(OptionsError, match="option 'vlm_model' needs the option pipeline set to 'vlm'"):
        parse_options({"vlm_model": "granite_docling"})
    with pytest.raises(OptionsError, match="'vlm_model' must be granite_docling or smoldocling"):
        parse_options({"pipeline": "vlm", "vlm_model": "dots_ocr"})
    with pytest.raises(OptionsError, match="option 'pipeline' must be standard or vlm"):
        parse_options({"pipeline": "fast"})


def test_vlm_options_cannot_be_set_through_engine(fake_docling: Any) -> None:
    with pytest.raises(OptionsError, match="use the option 'vlm_model'"):
        parse_options({"pipeline": "vlm", "engine": {"vlm_options": {}}})
    pipeline = build_pipeline_options(opts(pipeline="vlm", engine={"force_backend_text": True}))
    assert pipeline.force_backend_text is True and pipeline.vlm_options.preset == "granite_docling"
    with pytest.raises(OptionsError, match="has no field 'ocr_options'"):
        build_pipeline_options(opts(pipeline="vlm", engine={"ocr_options": {}}))  # standard-only field


def test_the_pipeline_is_part_of_the_converter_cache_key(fake_docling: Any) -> None:
    convert = DoclingConverter(Settings())
    for options in (opts(), opts(pipeline="vlm"), opts(pipeline="vlm", vlm_model="smoldocling")):
        convert(b"x", "a.pdf", options)
        convert(b"x", "a.pdf", options)
    assert len(fake_docling.instances) == 3
