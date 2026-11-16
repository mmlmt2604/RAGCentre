"""Tests for the PaddleOCR-VL shim. They need only fastapi, httpx and pytest: no PaddleOCR, no models."""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from shimkit import ShimError

from paddleocr_vl_shim import engine as engine_module
from paddleocr_vl_shim.app import NAME, create_app, service_version
from paddleocr_vl_shim.convert import LAYOUT_LABELS, PROMPT_LABELS, to_ir
from paddleocr_vl_shim.engine import (
    BACKENDS,
    ENGINE_INIT_ARGUMENTS,
    ENGINE_PREDICT_ARGUMENTS,
    NAMED_ARGUMENTS,
    SERVICE_SETTING_ARGUMENTS,
    PaddleVLEngine,
    RecognitionSettings,
    reduce_page,
    split_engine,
)

GOLDEN = Path(__file__).parent / "golden" / "sample_ir.json"
TABLE_HTML = "<table><tr><td>Flow</td></tr><tr><td>40 l/min</td></tr></table>"


def block(label: str, content: str, bbox: list[int], title_level: int | None = None) -> dict[str, Any]:
    return {"label": label, "content": content, "bbox": bbox, "title_level": title_level}


def sample() -> list[dict[str, Any]]:
    """The reduced shape ``engine.reduce_page`` produces, describing the two-page "Pump Manual" (pixel
    boxes on a 1224 x 1584 page). Between them the two pages carry every label PaddleOCR-VL can emit
    (``LAYOUT_LABELS`` and ``PROMPT_LABELS``), in reading order."""
    return [
        {
            "page": 1,
            "width": 1224,
            "height": 1584,
            "blocks": [
                block("header", "Acme Pumps", [100, 40, 400, 70]),
                block("header_image", "", [900, 30, 1100, 90]),  # dropped
                block("doc_title", "Pump Manual ", [100, 100, 700, 160]),
                block("abstract", "Abstract. This manual covers installation and service.", [100, 180, 1100, 230]),
                block("content", "1 Installation 2\n2 Specifications 3", [100, 250, 600, 320]),
                block("paragraph_title", "Installation ", [100, 340, 400, 380], title_level=1),
                block("text", "Mount the pump on a level surface.", [100, 400, 1100, 440]),
                block("vertical_text", "Side note, rotated", [1150, 400, 1190, 700]),
                block("algorithm", "if pressure > limit:\n    close_valve()", [100, 460, 800, 540]),
                block("display_formula", "E = mc^2", [100, 560, 500, 620]),
                block("formula_number", "(1)", [1000, 570, 1060, 610]),  # dropped
                block("inline_formula", "p_{in}", [100, 640, 200, 670]),
                block("footnote", "1 See the safety sheet.", [100, 1400, 600, 1430]),
                block("aside_text", "arXiv:0000.00000", [20, 300, 60, 900]),
                block("number", "1", [600, 1500, 640, 1530]),
                block("footer", "Confidential", [100, 1500, 400, 1530]),
                block("footer_image", "", [900, 1490, 1100, 1550]),  # dropped
            ],
        },
        {
            "page": 2,
            "width": 1224,
            "height": 1584,
            "blocks": [
                block("paragraph_title", "Specifications", [100, 80, 500, 120], title_level=2),
                block("table", TABLE_HTML, [100, 160, 600, 400]),
                block("figure_title", "Table 1: Ratings", [100, 410, 600, 440]),
                block("image", "", [100, 460, 600, 900]),
                block("figure_title", "Figure 1: exploded view", [100, 920, 600, 960]),
                block("chart", "Flow against head", [100, 980, 600, 1200]),
                block("seal", "ACME QA", [700, 980, 900, 1100]),
                block("vision_footnote", "Source: test bench.", [100, 1210, 600, 1240]),
                block("reference", "References\n[1] ISO 5199", [100, 1260, 600, 1330]),
                block("reference_content", "[2] API 610", [100, 1340, 600, 1370]),
                block("ocr", "Plain page text", [100, 1380, 600, 1410]),
                block("spotting", "Spotted text", [100, 1420, 600, 1450]),
                block("formula", "x + y", [100, 1460, 300, 1490]),
                block("table", "<fcel>Flow<fcel>40<nl>", [100, 1500, 300, 1540]),  # the engine's own fallback text
            ],
        },
    ]


def test_mapping() -> None:
    ir = to_ir(sample(), source_name="manual.pdf")
    assert ir["schema_version"] == "1.1"
    assert ir["metadata"] == {"page_count": 2, "title": "Pump Manual", "source_name": "manual.pdf"}
    assert ir["pages"] == [
        {"page": 1, "width": 1224.0, "height": 1584.0},
        {"page": 2, "width": 1224.0, "height": 1584.0},
    ]
    kinds = [(b["type"], b.get("level"), b["text"]) for b in ir["blocks"]]
    assert kinds[:7] == [
        ("page_header", None, "Acme Pumps"),
        ("heading", 1, "Pump Manual"),
        ("paragraph", None, "Abstract. This manual covers installation and service."),
        ("paragraph", None, "1 Installation 2\n2 Specifications 3"),
        ("heading", 2, "Installation"),
        ("paragraph", None, "Mount the pump on a level surface."),
        ("paragraph", None, "Side note, rotated"),
    ]
    table = next(b for b in ir["blocks"] if b["type"] == "table")
    assert table["text"] == "| Flow |\n| --- |\n| 40 l/min |"
    assert table["html"] == TABLE_HTML
    assert [b["id"] for b in ir["blocks"]] == [f"b{i}" for i in range(1, len(ir["blocks"]) + 1)]
    assert all(b["text"].strip() or b["type"] == "figure" for b in ir["blocks"])
    assert [b["page"] for b in ir["blocks"]] == sorted(b["page"] for b in ir["blocks"])  # reading order kept


def test_bbox_units() -> None:
    """Boxes are the engine's pixels and so are the page sizes: nothing is scaled."""
    ir = to_ir(sample())
    assert ir["blocks"][0]["bbox"] == [100.0, 40.0, 400.0, 70.0] and ir["blocks"][0]["page"] == 1
    for b in ir["blocks"]:
        page = next(p for p in ir["pages"] if p["page"] == b["page"])
        x0, y0, x1, y1 = b["bbox"]
        assert 0 <= x0 < x1 <= page["width"] and 0 <= y0 < y1 <= page["height"]


def test_block_without_bbox_has_none() -> None:
    pages = [{"page": 1, "width": 10, "height": 10, "blocks": [{"label": "text", "content": "x", "bbox": None}]}]
    assert "bbox" not in to_ir(pages)["blocks"][0]


# Every label the pipeline can emit, and what it becomes (the table in convert.py). ``None``: dropped.
EXPECTED: dict[str, dict[str, Any] | None] = {
    "abstract": {"type": "paragraph"},
    "algorithm": {"type": "code"},
    "aside_text": {"type": "aside_text"},
    "chart": {"type": "figure"},
    "content": {"type": "paragraph"},
    "display_formula": {"type": "formula"},
    "doc_title": {"type": "heading", "level": 1},
    "figure_title": {"type": "caption"},
    "footer": {"type": "page_footer"},
    "footer_image": None,
    "footnote": {"type": "footnote"},
    "formula_number": None,
    "header": {"type": "page_header"},
    "header_image": None,
    "image": {"type": "figure"},
    "inline_formula": {"type": "formula"},
    "number": {"type": "page_footer"},
    "paragraph_title": {"type": "heading", "level": 2},
    "reference": {"type": "paragraph"},
    "reference_content": {"type": "paragraph"},
    "seal": {"type": "figure"},
    "table": {"type": "table"},
    "text": {"type": "paragraph"},
    "vertical_text": {"type": "paragraph"},
    "vision_footnote": {"type": "footnote"},
    "ocr": {"type": "paragraph"},
    "spotting": {"type": "paragraph"},
    "formula": {"type": "formula"},
}


def test_every_label_is_in_the_table() -> None:
    labels = set(LAYOUT_LABELS) | set(PROMPT_LABELS)
    assert set(EXPECTED) == labels
    assert len(LAYOUT_LABELS) == 25 and len(set(LAYOUT_LABELS)) == 25  # the layout models' label_list
    assert {raw["label"] for page in sample() for raw in page["blocks"]} == labels  # and the golden carries each


@pytest.mark.parametrize("label", sorted(EXPECTED))
def test_label_mapping(label: str) -> None:
    pages = [{"page": 1, "width": 10, "height": 10, "blocks": [{"label": label, "content": "x", "bbox": [1, 2, 3, 4]}]}]
    blocks = to_ir(pages)["blocks"]
    expected = EXPECTED[label]
    if expected is None:
        assert blocks == []
        return
    (got,) = blocks
    assert {k: got[k] for k in expected} == expected
    assert got["page"] == 1 and got["bbox"] == [1.0, 2.0, 3.0, 4.0] and got["text"] == "x"


def test_a_label_the_table_does_not_know_keeps_its_name() -> None:
    pages = [{"page": 1, "width": 10, "height": 10, "blocks": [{"label": "something_new", "content": "x"}]}]
    (got,) = to_ir(pages)["blocks"]
    assert got["type"] == "something_new" and got["text"] == "x"


@pytest.mark.parametrize(
    ("title_level", "level"), [(None, 2), (0, 2), (1, 2), (2, 3), (5, 6), (6, 6), (40, 6), (True, 2), ("2", 2)]
)
def test_section_title_levels(title_level: Any, level: int) -> None:
    pages = [
        {
            "page": 1,
            "width": 10,
            "height": 10,
            "blocks": [{"label": "paragraph_title", "content": "S", "title_level": title_level}],
        }
    ]
    assert to_ir(pages)["blocks"][0]["level"] == level


def test_doc_title_is_level_one_whatever_title_level_says() -> None:
    pages = [
        {"page": 1, "width": 10, "height": 10, "blocks": [{"label": "doc_title", "content": "T", "title_level": 3}]}
    ]
    assert to_ir(pages)["blocks"][0]["level"] == 1


def test_table_without_html_keeps_the_engines_text_and_has_no_html() -> None:
    """When the engine cannot turn the model's table tokens into HTML it keeps the tokens."""
    pages = [{"page": 1, "width": 10, "height": 10, "blocks": [{"label": "table", "content": "<fcel>Flow<nl> "}]}]
    (table,) = to_ir(pages)["blocks"]
    assert table["type"] == "table" and table["text"] == "<fcel>Flow<nl>" and "html" not in table


def test_table_html_that_has_no_rows_falls_back_to_text() -> None:
    pages = [
        {"page": 1, "width": 10, "height": 10, "blocks": [{"label": "table", "content": "<table>just text</table>"}]}
    ]
    (table,) = to_ir(pages)["blocks"]
    assert table["text"] == "just text" and table["html"] == "<table>just text</table>"


def test_table_content_with_html_and_body_wrappers_keeps_only_the_table() -> None:
    wrapped = f"<html><body>{TABLE_HTML}</body></html>"
    pages = [{"page": 1, "width": 10, "height": 10, "blocks": [{"label": "table", "content": wrapped}]}]
    assert to_ir(pages)["blocks"][0]["html"] == TABLE_HTML


def test_pages_are_sorted_and_ids_are_global() -> None:
    ir = to_ir(sample()[::-1])
    assert [p["page"] for p in ir["pages"]] == [1, 2]
    assert ir["blocks"][0]["text"] == "Acme Pumps" and ir["blocks"][0]["id"] == "b1"


def test_matches_golden() -> None:
    """``tests/golden/sample_ir.json`` is this sample's IR. RAGCentre's own test suite can validate the
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
            raise ShimError(503, "engine_unavailable", "cannot start PaddleOCR-VL")
        return sample()

    return run


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADDLEOCR_VL_SHIM_API_KEY", raising=False)
    for variable in SERVICE_SETTING_ARGUMENTS.values():
        monkeypatch.delenv(variable, raising=False)


def post(client: TestClient, name: str = "m.pdf", options: Any = None) -> Any:
    data = {} if options is None else {"options": options if isinstance(options, str) else json.dumps(options)}
    return client.post("/parse", files={"file": (name, b"%PDF", "application/pdf")}, data=data)


DEFAULTS = {
    "layout_detection": True,
    "orientation": False,
    "unwarp": False,
    "charts": False,
    "seals": False,
    "image_ocr": False,
    "heading_levels": True,
    "merge_tables": True,
    "engine": {},
}


def test_unknown_option_is_400_and_named() -> None:
    calls: list[Any] = []
    client = TestClient(create_app(fake_engine(calls)))
    resp = post(client, options={"charts": True, "chartss": 1})
    assert resp.status_code == 400
    assert resp.json() == {"error": {"code": "invalid_options", "message": "option 'chartss' is not recognised"}}
    assert post(client, options={"charts": "false"}).status_code == 400
    assert calls == []


def test_info_reports_the_options_schema() -> None:
    schema = TestClient(create_app(fake_engine([]))).get("/info").json()["options_schema"]
    assert schema["additionalProperties"] is False
    assert list(schema["properties"]) == list(DEFAULTS)
    assert all(schema["properties"][name].get("description") for name in DEFAULTS)


def test_service_endpoints() -> None:
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_engine(calls)))
    info = client.get("/info").json()
    assert info["protocol"] == "ragcentre.parser/v1" and info["name"] == NAME == "paddleocr-vl-shim"
    assert info["version"].startswith("0.1.0+paddleocr.") and ".vl-v1.6" in info["version"]
    assert info["mime_types"] == ["application/pdf", "image/png", "image/jpeg", "image/bmp", "image/tiff", "image/webp"]
    assert info["capabilities"] == {"page_images": False}

    resp = post(client, options={"charts": True, "heading_levels": False})
    assert resp.status_code == 200
    ir = resp.json()
    assert ir["blocks"][0]["text"] == "Acme Pumps" and ir["metadata"]["source_name"] == "m.pdf"
    assert calls[0] == (b"%PDF", "m.pdf", {**DEFAULTS, "charts": True, "heading_levels": False})
    assert post(client).status_code == 200
    assert calls[1][2] == DEFAULTS  # no options: the defaults

    bad = post(client, options='["not an object"]')
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_options"
    wrong_type = post(client, options={"charts": "yes"})
    assert wrong_type.status_code == 400 and "charts" in wrong_type.json()["error"]["message"]
    unsupported = post(client, name="notes.txt")
    assert unsupported.status_code == 415 and unsupported.json()["error"]["code"] == "unsupported_media_type"
    assert len(calls) == 2  # the 400s and the 415 never reached the engine
    broken = post(client, name="m.bad.pdf")
    assert broken.status_code == 422 and broken.json()["error"]["code"] == "conversion_failed"
    assert "broken file" in broken.json()["error"]["message"]
    down = post(client, name="m.down.pdf")
    assert down.status_code == 503 and down.json()["error"]["code"] == "engine_unavailable"


@pytest.mark.parametrize(
    "name", ["scan.PNG", "scan.jpg", "scan.jpeg", "scan.bmp", "scan.tif", "scan.TIFF", "scan.webp"]
)
def test_image_extensions_are_accepted(name: str) -> None:
    assert post(TestClient(create_app(fake_engine([]))), name=name).status_code == 200


@pytest.mark.parametrize("name", ["a.docx", "a.html", "a.gif", "a", "a.pdf.exe"])
def test_other_extensions_are_415(name: str) -> None:
    assert post(TestClient(create_app(fake_engine([]))), name=name).status_code == 415


@pytest.mark.parametrize(
    "options",
    [
        {"page_range": [1, 2]},  # PaddleOCR-VL has no page selection, so the option does not exist
        {"languages": ["en"]},  # and it is multilingual: there is no language to choose
        {"layout_detection": "yes"},
        {"merge_tables": 1},
        {"engine": []},
    ],
)
def test_options_that_do_not_exist_or_have_the_wrong_type_are_400(options: dict[str, Any]) -> None:
    resp = post(TestClient(create_app(fake_engine([]))), options=options)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_options"
    assert f"'{next(iter(options))}'" in resp.json()["error"]["message"]


def test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_API_KEY", "s3cret")
    client = TestClient(create_app(fake_engine([])))
    assert client.get("/info").status_code == 200  # RAGCentre reads /info without credentials
    assert post(client).status_code == 401
    ok = client.post(
        "/parse", files={"file": ("m.pdf", b"x", "application/pdf")}, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


# --- service settings and the version ------------------------------------------------------------


def test_version_names_the_pipeline_generation() -> None:
    assert service_version().endswith(".vl-v1.6") and service_version().startswith("0.1.0+paddleocr.")
    assert service_version(RecognitionSettings(pipeline_version="v1.5")).endswith(".vl-v1.5")


def test_version_carries_the_backend_and_the_model_names() -> None:
    settings = RecognitionSettings(
        backend="llama-cpp-server", server_url="http://h:8080/v1", model="PaddleOCR-VL-1.6-0.9B", api_model="ocr/vl 1"
    )
    assert service_version(settings).endswith(
        ".vl-v1.6.backend-llama-cpp-server.model-PaddleOCR-VL-1-6-0-9B.apimodel-ocr-vl-1"
    )
    assert ".backend-" not in service_version(RecognitionSettings(backend="native"))  # native is the default


def test_the_url_the_key_and_the_concurrency_are_never_in_the_version_or_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_REC_BACKEND", "vllm-server")
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_REC_SERVER_URL", "http://internal-host:8118/v1")
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_REC_API_KEY", "k3y-value")
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_REC_MAX_CONCURRENCY", "7")
    info = TestClient(create_app(fake_engine([]))).get("/info")
    assert info.json()["version"].endswith(".backend-vllm-server")
    for secret in ("internal-host", "8118", "k3y-value"):
        assert secret not in info.text
    assert service_version() == info.json()["version"]


def test_settings_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert RecognitionSettings.from_env() == RecognitionSettings()
    assert RecognitionSettings().constructor_arguments() == {"pipeline_version": "v1.6"}
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_PIPELINE_VERSION", "v1.5")
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_REC_BACKEND", "llama-cpp-server")
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_REC_SERVER_URL", " http://127.0.0.1:8080/v1 ")
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_REC_MODEL", "PaddleOCR-VL-1.5-0.9B")
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_REC_API_MODEL", "paddleocr-vl")
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_REC_API_KEY", "k")
    monkeypatch.setenv("PADDLEOCR_VL_SHIM_REC_MAX_CONCURRENCY", "4")
    assert RecognitionSettings.from_env().constructor_arguments() == {
        "pipeline_version": "v1.5",
        "vl_rec_backend": "llama-cpp-server",
        "vl_rec_server_url": "http://127.0.0.1:8080/v1",
        "vl_rec_model_name": "PaddleOCR-VL-1.5-0.9B",
        "vl_rec_api_model_name": "paddleocr-vl",
        "vl_rec_api_key": "k",
        "vl_rec_max_concurrency": 4,
    }


@pytest.mark.parametrize(
    ("settings", "variable"),
    [
        (RecognitionSettings(pipeline_version="v9"), "PADDLEOCR_VL_SHIM_PIPELINE_VERSION"),
        (RecognitionSettings(backend="cloud"), "PADDLEOCR_VL_SHIM_REC_BACKEND"),
        (RecognitionSettings(backend="vllm-server"), "PADDLEOCR_VL_SHIM_REC_SERVER_URL"),
        (RecognitionSettings(server_url="http://x/v1"), "PADDLEOCR_VL_SHIM_REC_SERVER_URL"),
        (RecognitionSettings(api_model="m"), "PADDLEOCR_VL_SHIM_REC_API_MODEL"),
        (RecognitionSettings(api_key="k"), "PADDLEOCR_VL_SHIM_REC_API_KEY"),
        (RecognitionSettings(max_concurrency="3"), "PADDLEOCR_VL_SHIM_REC_MAX_CONCURRENCY"),
        (RecognitionSettings(max_concurrency="0"), "PADDLEOCR_VL_SHIM_REC_MAX_CONCURRENCY"),
        (RecognitionSettings(max_concurrency="many"), "PADDLEOCR_VL_SHIM_REC_MAX_CONCURRENCY"),
    ],
)
def test_a_wrong_service_setting_is_503_and_names_the_variable(settings: RecognitionSettings, variable: str) -> None:
    with pytest.raises(ShimError) as err:
        settings.constructor_arguments()
    assert err.value.status == 503 and err.value.code == "engine_unavailable" and variable in err.value.message


def test_every_backend_but_native_needs_a_server_url() -> None:
    for backend in BACKENDS:
        settings = RecognitionSettings(backend=backend, server_url="http://x/v1")
        if backend == "native":
            with pytest.raises(ShimError):
                settings.constructor_arguments()
        else:
            assert settings.constructor_arguments()["vl_rec_backend"] == backend


# --- engine (with a stand-in for the paddleocr package) ------------------------------------------


class FakeResult(dict[str, Any]):
    pass


def fake_result(page_index: int | None, blocks: list[types.SimpleNamespace]) -> FakeResult:
    return FakeResult(page_index=page_index, page_count=None if page_index is None else 2, width=1224, height=1584,
                      parsing_res_list=blocks)  # fmt: skip


def layout_block(label: str, content: str, bbox: list[int], **extra: Any) -> types.SimpleNamespace:
    # PaddleOCRVLBlock has no ``order_index``; ``title_level`` is only there after ``restructure_pages``
    return types.SimpleNamespace(label=label, content=content, bbox=bbox, group_id=None, **extra)


class FakePipeline:
    instances: ClassVar[list[FakePipeline]] = []

    def __init__(self, **kwargs: Any) -> None:
        if kwargs.get("vl_rec_model_name") == "boom":
            raise ValueError("no such model")
        self.kwargs = kwargs
        self.predict_calls: list[tuple[Path, dict[str, Any], bytes]] = []
        self.restructure_calls: list[dict[str, Any]] = []
        self.closed = 0
        FakePipeline.instances.append(self)

    def predict(self, path: str, **kwargs: Any) -> list[FakeResult]:
        self.predict_calls.append((Path(path), kwargs, Path(path).read_bytes()))
        return [fake_result(0, [layout_block("paragraph_title", "Installation", [1, 2, 3, 4])])]

    def restructure_pages(
        self, res_list: list[FakeResult], merge_tables: bool = True, relevel_titles: bool = True, **_: Any
    ) -> list[FakeResult]:
        self.restructure_calls.append({"merge_tables": merge_tables, "relevel_titles": relevel_titles})
        if relevel_titles:
            for result in res_list:
                for blk in result["parsing_res_list"]:
                    if blk.label == "paragraph_title":
                        blk.title_level = 1
        return res_list

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def fake_paddleocr(monkeypatch: pytest.MonkeyPatch) -> type[FakePipeline]:
    FakePipeline.instances = []
    module = types.ModuleType("paddleocr")
    module.PaddleOCRVL = FakePipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    return FakePipeline


OPTIONS = DEFAULTS
FLAGS = {
    "use_doc_orientation_classify": False,
    "use_doc_unwarping": False,
    "use_layout_detection": True,
    "use_chart_recognition": False,
    "use_seal_recognition": False,
    "use_ocr_for_image_block": False,
}


def test_reduce_page() -> None:
    result = fake_result(
        0,
        [
            layout_block("paragraph_title", "T", [1, 2, 3, 4], title_level=2),
            layout_block("table", "<table/>", [5, 6, 7, 8]),
        ],
    )
    assert reduce_page(result, 1) == {
        "page": 1,
        "width": 1224,
        "height": 1584,
        "blocks": [
            {"label": "paragraph_title", "content": "T", "bbox": [1, 2, 3, 4], "title_level": 2},
            {"label": "table", "content": "<table/>", "bbox": [5, 6, 7, 8], "title_level": None},
        ],
    }


def test_reduce_page_uses_the_position_when_page_index_is_none() -> None:
    """A single image has ``page_index`` None (confirmed live against PaddleOCR 3.7.0)."""
    assert reduce_page(fake_result(None, []), 1)["page"] == 1
    assert reduce_page(fake_result(None, []), 3)["page"] == 3
    assert reduce_page(fake_result(1, []), 1)["page"] == 2  # page_index is 0-based for PDFs


def test_engine_passes_options_to_the_constructor_and_predict(fake_paddleocr: type[FakePipeline]) -> None:
    engine = PaddleVLEngine(RecognitionSettings())
    pages = engine(b"%PDF-data", "m.PDF", {**OPTIONS, "seals": True, "orientation": True})
    (pipeline,) = fake_paddleocr.instances
    expected = {**FLAGS, "use_seal_recognition": True, "use_doc_orientation_classify": True}
    assert pipeline.kwargs == {"pipeline_version": "v1.6", **expected}  # features must be on when it is built
    (path, kwargs, content) = pipeline.predict_calls[0]
    assert kwargs == expected and content == b"%PDF-data" and path.suffix == ".pdf"
    assert not path.exists()  # the temporary file is removed
    assert pages == [
        {
            "page": 1,
            "width": 1224,
            "height": 1584,
            "blocks": [{"label": "paragraph_title", "content": "Installation", "bbox": [1, 2, 3, 4], "title_level": 1}],
        }
    ]


def test_the_backend_settings_reach_the_constructor_only(fake_paddleocr: type[FakePipeline]) -> None:
    settings = RecognitionSettings(
        backend="llama-cpp-server", server_url="http://127.0.0.1:8080/v1", api_key="k", max_concurrency="2"
    )
    PaddleVLEngine(settings)(b"x", "a.pdf", OPTIONS)
    (pipeline,) = fake_paddleocr.instances
    assert pipeline.kwargs["vl_rec_backend"] == "llama-cpp-server"
    assert pipeline.kwargs["vl_rec_server_url"] == "http://127.0.0.1:8080/v1"
    assert pipeline.kwargs["vl_rec_api_key"] == "k" and pipeline.kwargs["vl_rec_max_concurrency"] == 2
    assert pipeline.predict_calls[0][1] == FLAGS  # predict gets neither the URL nor the key


def test_heading_levels_and_merge_tables_run_the_engines_second_step(fake_paddleocr: type[FakePipeline]) -> None:
    engine = PaddleVLEngine(RecognitionSettings())
    levelled = engine(b"x", "a.pdf", OPTIONS)
    assert levelled[0]["blocks"][0]["title_level"] == 1
    (pipeline,) = fake_paddleocr.instances
    assert pipeline.restructure_calls == [{"merge_tables": True, "relevel_titles": True}]

    only_merge = engine(b"x", "a.pdf", {**OPTIONS, "heading_levels": False})
    assert pipeline.restructure_calls[-1] == {"merge_tables": True, "relevel_titles": False}
    assert only_merge[0]["blocks"][0]["title_level"] is None

    engine(b"x", "a.pdf", {**OPTIONS, "merge_tables": False})
    assert pipeline.restructure_calls[-1] == {"merge_tables": False, "relevel_titles": True}

    calls = len(pipeline.restructure_calls)
    plain = engine(b"x", "a.pdf", {**OPTIONS, "heading_levels": False, "merge_tables": False})
    assert len(pipeline.restructure_calls) == calls  # neither: the second step is skipped
    assert plain[0]["blocks"][0]["title_level"] is None
    assert len(fake_paddleocr.instances) == 1  # neither is a pipeline argument


def test_predict_alone_leaves_title_level_unset_so_headings_are_level_two(fake_paddleocr: type[FakePipeline]) -> None:
    pages = PaddleVLEngine(RecognitionSettings())(b"x", "a.pdf", {**OPTIONS, "heading_levels": False})
    assert to_ir(pages)["blocks"][0]["level"] == 2
    pages = PaddleVLEngine(RecognitionSettings())(b"x", "a.pdf", OPTIONS)  # title_level 1 -> level 2
    assert to_ir(pages)["blocks"][0]["level"] == 2


def test_a_failure_in_the_second_step_is_not_hidden(
    fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(self: FakePipeline, res_list: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no scikit-learn")

    monkeypatch.setattr(FakePipeline, "restructure_pages", explode)
    resp = post(TestClient(create_app(PaddleVLEngine(RecognitionSettings()))))
    assert resp.status_code == 422 and "no scikit-learn" in resp.json()["error"]["message"]


def test_no_results_skips_the_second_step(fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakePipeline, "predict", lambda self, path, **kwargs: [])
    assert PaddleVLEngine(RecognitionSettings())(b"x", "a.pdf", OPTIONS) == []
    assert fake_paddleocr.instances[0].restructure_calls == []


def test_engine_keeps_one_pipeline_per_option_combination(fake_paddleocr: type[FakePipeline]) -> None:
    engine = PaddleVLEngine(RecognitionSettings())
    engine(b"x", "a.pdf", OPTIONS)
    engine(b"x", "b.pdf", OPTIONS)
    assert len(fake_paddleocr.instances) == 1 and len(fake_paddleocr.instances[0].predict_calls) == 2
    engine(b"x", "c.pdf", {**OPTIONS, "charts": True})
    assert len(fake_paddleocr.instances) == 2


def test_engine_start_failure_is_503(fake_paddleocr: type[FakePipeline]) -> None:
    with pytest.raises(ShimError) as err:
        PaddleVLEngine(RecognitionSettings(model="boom"))(b"x", "a.pdf", OPTIONS)
    assert err.value.status == 503 and err.value.code == "engine_unavailable" and "no such model" in err.value.message


def test_engine_missing_paddleocr_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "paddleocr", None)  # makes ``import paddleocr`` raise ImportError
    with pytest.raises(ShimError) as err:
        PaddleVLEngine(RecognitionSettings())(b"x", "a.pdf", OPTIONS)
    assert err.value.status == 503


def test_a_misconfigured_backend_is_503_before_paddleocr_is_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "paddleocr", None)
    with pytest.raises(ShimError) as err:
        PaddleVLEngine(RecognitionSettings(backend="vllm-server"))(b"x", "a.pdf", OPTIONS)
    assert "PADDLEOCR_VL_SHIM_REC_SERVER_URL" in err.value.message


def test_predict_failure_propagates_for_a_422(
    fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(self: FakePipeline, path: str, **kwargs: Any) -> Any:
        raise RuntimeError("bad page")

    monkeypatch.setattr(FakePipeline, "predict", explode)
    resp = post(TestClient(create_app(PaddleVLEngine(RecognitionSettings()))))
    assert resp.status_code == 422 and "bad page" in resp.json()["error"]["message"]


def test_an_unreachable_server_is_503_and_names_the_setting_not_the_url(
    fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: FakePipeline, path: str, **kwargs: Any) -> Any:
        # what PaddleX raises through its worker queue (read live): the original class is lost
        raise RuntimeError("Exception from the 'vlm' worker: Connection error.")

    monkeypatch.setattr(FakePipeline, "predict", refuse)
    probed: list[str] = []
    monkeypatch.setattr(engine_module, "server_reachable", lambda url: probed.append(url) or False)
    settings = RecognitionSettings(backend="llama-cpp-server", server_url="http://internal-host:9/v1")
    resp = post(TestClient(create_app(PaddleVLEngine(settings))))
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "engine_unavailable"
    message = resp.json()["error"]["message"]
    assert "PADDLEOCR_VL_SHIM_REC_SERVER_URL" in message and "internal-host" not in message
    assert probed == ["http://internal-host:9/v1"]
    # a failure that is not the recognition worker's, with the server up, is an ordinary one
    monkeypatch.setattr(engine_module, "server_reachable", lambda url: True)
    failing_predict(monkeypatch, RuntimeError("bad page"))
    resp = post(TestClient(create_app(PaddleVLEngine(settings))))
    assert resp.status_code == 422 and "bad page" in resp.json()["error"]["message"]
    monkeypatch.setattr(engine_module, "server_reachable", lambda url: pytest.fail("native has no server"))
    assert post(TestClient(create_app(PaddleVLEngine(RecognitionSettings())))).status_code == 422


def test_server_reachable_connects_to_the_host_and_port() -> None:
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert engine_module.server_reachable(f"http://127.0.0.1:{port}/v1")
    assert not engine_module.server_reachable(f"http://127.0.0.1:{port}/v1")  # closed again
    assert not engine_module.server_reachable("not a url")


def test_the_pipeline_cache_is_bounded_least_recently_used_and_closes_what_it_drops(
    fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert engine_module.MAX_PIPELINES == 4
    monkeypatch.setattr(engine_module, "MAX_PIPELINES", 2)
    engine = PaddleVLEngine(RecognitionSettings())
    a, b, c = OPTIONS, {**OPTIONS, "charts": True}, {**OPTIONS, "seals": True}
    engine(b"x", "a.pdf", a)
    engine(b"x", "a.pdf", b)
    engine(b"x", "a.pdf", a)  # a is now the most recently used
    engine(b"x", "a.pdf", c)  # evicts b
    assert len(fake_paddleocr.instances) == 3
    assert [p.closed for p in fake_paddleocr.instances] == [0, 1, 0]
    engine(b"x", "a.pdf", a)  # still cached
    assert len(fake_paddleocr.instances) == 3
    engine(b"x", "a.pdf", b)  # was evicted: built again
    assert len(fake_paddleocr.instances) == 4
    engine.close()
    assert [p.closed for p in fake_paddleocr.instances] == [1, 1, 1, 1]
    engine.close()  # nothing left to close
    assert [p.closed for p in fake_paddleocr.instances] == [1, 1, 1, 1]


def test_the_app_closes_the_pipelines_when_it_stops(fake_paddleocr: type[FakePipeline]) -> None:
    with TestClient(create_app(settings=RecognitionSettings())) as client:  # runs the lifespan
        assert post(client).status_code == 200
        (pipeline,) = fake_paddleocr.instances
        assert pipeline.closed == 0
    assert pipeline.closed == 1


# --- the engine passthrough ---------------------------------------------------------------------


def test_engine_arguments_go_to_the_constructor_and_predict_by_signature(fake_paddleocr: type[FakePipeline]) -> None:
    engine = {
        "layout_detection_model_name": "PP-DocLayoutV2",  # constructor only
        "temperature": 0.1,  # predict only
        "max_new_tokens": 512,  # predict only
        "layout_threshold": 0.3,  # both
        "merge_layout_blocks": False,  # both
    }
    PaddleVLEngine(RecognitionSettings())(b"x", "a.pdf", {**OPTIONS, "engine": engine})
    (pipeline,) = fake_paddleocr.instances
    assert pipeline.kwargs == {
        "pipeline_version": "v1.6",
        **FLAGS,
        "layout_detection_model_name": "PP-DocLayoutV2",
        "layout_threshold": 0.3,
        "merge_layout_blocks": False,
    }
    assert pipeline.predict_calls[0][1] == {
        **FLAGS,
        "temperature": 0.1,
        "max_new_tokens": 512,
        "layout_threshold": 0.3,
        "merge_layout_blocks": False,
    }


def test_sampling_settings_are_reachable() -> None:
    sampling = {"temperature", "top_p", "repetition_penalty", "max_new_tokens", "min_pixels", "max_pixels"}
    assert sampling <= ENGINE_PREDICT_ARGUMENTS and not sampling & ENGINE_INIT_ARGUMENTS
    assert {"prompt_label", "vlm_extra_args"} <= ENGINE_PREDICT_ARGUMENTS
    init, predict = split_engine({"top_p": 0.9, "vlm_extra_args": {"ocr_max_pixels": 1000000}})
    assert init == {} and predict == {"top_p": 0.9, "vlm_extra_args": {"ocr_max_pixels": 1000000}}


@pytest.mark.parametrize(
    ("engine", "text"),
    [
        ({"layout_detection_model_dir": "/x"}, "'layout_detection_model_dir'"),
        ({"vl_rec_model_dir": "/x"}, "'vl_rec_model_dir'"),
        ({"doc_unwarping_model_dir": "/x"}, "'doc_unwarping_model_dir'"),
        ({"vlm_extra_args": {"api_key": "k"}}, "'vlm_extra_args.api_key'"),
        ({"vlm_extra_args": {"headers": {}}}, "'vlm_extra_args.headers'"),
        ({"kwargs": {}}, "'kwargs'"),
        ({"input": "/etc/passwd"}, "'input'"),
        # the service settings: shimkit's denylist alone would let the URL and the key through
        ({"vl_rec_server_url": "http://x/v1"}, "service setting (PADDLEOCR_VL_SHIM_REC_SERVER_URL)"),
        ({"vl_rec_api_key": "k"}, "service setting (PADDLEOCR_VL_SHIM_REC_API_KEY)"),
        ({"vl_rec_backend": "native"}, "service setting (PADDLEOCR_VL_SHIM_REC_BACKEND)"),
        ({"vl_rec_model_name": "m"}, "service setting (PADDLEOCR_VL_SHIM_REC_MODEL)"),
        ({"vl_rec_api_model_name": "m"}, "service setting (PADDLEOCR_VL_SHIM_REC_API_MODEL)"),
        ({"vl_rec_max_concurrency": 9}, "service setting (PADDLEOCR_VL_SHIM_REC_MAX_CONCURRENCY)"),
        ({"pipeline_version": "v1"}, "service setting (PADDLEOCR_VL_SHIM_PIPELINE_VERSION)"),
        ({"use_chart_recognition": True}, "use the option 'charts'"),
        ({"use_layout_detection": False}, "use the option 'layout_detection'"),
        ({"use_ocr_for_image_block": True}, "use the option 'image_ocr'"),
        ({"use_doc_unwarping": True}, "use the option 'unwarp'"),
        ({"use_queues": True}, "no PaddleOCRVL argument 'use_queues'"),
        ({"format_block_content": True}, "no PaddleOCRVL argument 'format_block_content'"),
        ({"markdown_ignore_labels": []}, "no PaddleOCRVL argument 'markdown_ignore_labels'"),
        ({"temprature": 0.1}, "no PaddleOCRVL argument 'temprature'"),
    ],
)
def test_engine_refuses_denied_service_named_and_unknown_arguments(engine: dict[str, Any], text: str) -> None:
    with pytest.raises(ShimError) as err:
        split_engine(engine)
    assert err.value.status == 400 and err.value.code == "invalid_options" and text in err.value.message


def test_the_service_answers_a_bad_engine_with_400_before_the_engine_runs() -> None:
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_engine(calls)))
    resp = post(client, options={"engine": {"vl_rec_server_url": "http://x/v1"}})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_options"
    assert post(client, options={"engine": {"layout_threshold": 0.3}}).status_code == 200
    assert len(calls) == 1


def test_merged_table_preserves_all_page_locations(
    fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = layout_block("table", TABLE_HTML, [1, 2, 3, 4])
    second = layout_block("table", TABLE_HTML, [5, 6, 7, 8])
    monkeypatch.setattr(
        FakePipeline, "predict", lambda *args, **kwargs: [fake_result(0, [first]), fake_result(1, [second])]
    )

    def restructure(self: FakePipeline, results: Any, **kwargs: Any) -> Any:
        first.global_group_id = second.global_group_id = 0
        second.content = ""
        return results

    monkeypatch.setattr(FakePipeline, "restructure_pages", restructure)
    pages = PaddleVLEngine(RecognitionSettings())(b"x", "a.pdf", OPTIONS)
    (table,) = to_ir(pages)["blocks"]
    assert table["page"] == 1
    assert table["spans"] == [{"page": 1, "bbox": [1, 2, 3, 4]}, {"page": 2, "bbox": [5, 6, 7, 8]}]


def test_backend_errors_redact_service_settings() -> None:
    settings = RecognitionSettings(
        backend="vllm-server", server_url="http://private.invalid:8118/v1", api_key="private-secret"
    )

    def fail(*args: Any) -> Any:
        raise ShimError(503, "engine_unavailable", f"{settings.server_url}: rejected {settings.api_key}")

    response = post(TestClient(create_app(fail, settings)))
    assert response.status_code == 503
    assert settings.server_url not in response.text and settings.api_key not in response.text
    assert "[redacted]" in response.text
    # the bare host and port, as a connection error would echo it, goes too
    leaky = TestClient(
        create_app(
            lambda *a: (_ for _ in ()).throw(RuntimeError("failed to connect to private.invalid:8118")), settings
        )
    )
    assert "private.invalid" not in post(leaky).text


def test_service_settings_are_refused_in_nested_extra_args() -> None:
    with pytest.raises(ShimError, match=r"vlm_extra_args\.vl_rec_api_key"):
        split_engine({"vlm_extra_args": [{"vl_rec_api_key": "secret"}]})


def test_engine_validation_errors_are_400(fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("invalid temperature")

    monkeypatch.setattr(FakePipeline, "predict", fail)
    response = post(TestClient(create_app()), options={"engine": {"temperature": "bad"}})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_options"
    assert "invalid temperature" in response.text


def test_a_service_setting_that_is_wrong_stops_the_service_from_starting() -> None:
    for settings in (
        RecognitionSettings(pipeline_version="v9"),
        RecognitionSettings(backend="vllm-server"),  # no URL
        RecognitionSettings(api_key="k"),  # native has no server
    ):
        with pytest.raises(RuntimeError, match="invalid service settings: PADDLEOCR_VL_SHIM_"):
            create_app(fake_engine([]), settings)
    create_app(fake_engine([]), RecognitionSettings())  # the defaults are fine


def test_native_ignores_nothing_it_is_given() -> None:
    assert RecognitionSettings(model="PaddleOCR-VL-1.6-0.9B").constructor_arguments()["vl_rec_model_name"]


# What PaddleX raises through a worker thread: the original class is lost, only the message is kept.
def worker_failure(name: str, message: str = "Connection error.") -> RuntimeError:
    return RuntimeError(f"Exception from the '{name}' worker: {message}")


def failing_predict(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fail(self: FakePipeline, path: str, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(FakePipeline, "predict", fail)


SERVER = RecognitionSettings(backend="llama-cpp-server", server_url="http://127.0.0.1:8080/v1")


def test_a_failing_recognition_server_that_is_up_is_503_so_it_is_retried(
    fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "server_reachable", lambda url: True)
    failing_predict(monkeypatch, worker_failure("vlm", "Error code: 503 - overloaded"))
    resp = post(TestClient(create_app(PaddleVLEngine(SERVER))))
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "engine_unavailable"
    assert "recognition server failed" in resp.json()["error"]["message"] and "overloaded" in resp.text
    assert "127.0.0.1" not in resp.text  # redacted like every message


def test_a_layout_worker_failure_is_not_blamed_on_the_server(
    fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "server_reachable", lambda url: True)
    failing_predict(monkeypatch, worker_failure("cv", "cannot decode the page"))
    resp = post(TestClient(create_app(PaddleVLEngine(SERVER))))
    assert resp.status_code == 422 and "cannot decode the page" in resp.text


def test_a_native_worker_failure_is_a_422(fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch) -> None:
    failing_predict(monkeypatch, worker_failure("vlm", "out of memory"))
    assert post(TestClient(create_app(PaddleVLEngine(RecognitionSettings())))).status_code == 422


def test_only_errors_raised_by_predict_itself_for_engine_arguments_are_400(
    fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(PaddleVLEngine(RecognitionSettings())))
    failing_predict(monkeypatch, AssertionError("'prompt_label' must be one of ['ocr', ...]"))
    given = post(client, options={"layout_detection": False, "engine": {"prompt_label": "nope"}})
    assert given.status_code == 400 and given.json()["error"]["code"] == "invalid_options"
    # the same exception with no ``engine`` arguments is not the caller's options: a file the engine
    # cannot read, say, so it is not reported as an options problem
    assert post(client).status_code == 422
    # and a bad value that fails inside a worker arrives as a RuntimeError: a 422, not a 400
    failing_predict(monkeypatch, worker_failure("vlm", "temperature must be a number"))
    assert post(client, options={"engine": {"temperature": "hot"}}).status_code == 422


def test_argument_sets_do_not_overlap() -> None:
    allowed = ENGINE_INIT_ARGUMENTS | ENGINE_PREDICT_ARGUMENTS
    assert not set(NAMED_ARGUMENTS) & allowed
    assert not set(SERVICE_SETTING_ARGUMENTS) & allowed
    assert not set(SERVICE_SETTING_ARGUMENTS) & set(NAMED_ARGUMENTS)
    assert not allowed & set(engine_module.NOT_EXPOSED)
