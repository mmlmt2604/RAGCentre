"""Tests for the PaddleOCR shim. They need only fastapi, httpx and pytest: no PaddleOCR, no models."""

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

from paddleocr_shim.app import NAME, SHIM_VERSION, create_app, service_version
from paddleocr_shim.convert import to_ir
from paddleocr_shim.engine import PaddleEngine, reduce_page

GOLDEN = Path(__file__).parent / "golden" / "sample_ir.json"
TABLE_HTML = "<html><body><table><tr><td>Flow</td></tr><tr><td>40 l/min</td></tr></table></body></html>"


def block(
    label: str, content: str, bbox: list[int], order: int | None = 1, title_level: int | None = None
) -> dict[str, Any]:
    return {"label": label, "content": content, "bbox": bbox, "order": order, "title_level": title_level}


def sample() -> list[dict[str, Any]]:
    """The reduced shape ``engine.reduce_page`` produces (see convert.py for the engine source it
    follows), describing the two-page "Pump Manual". Pixel boxes on a 1200 x 1600 page."""
    return [
        {
            "page": 1,
            "width": 1200,
            "height": 1600,
            "blocks": [
                block("doc_title", "Pump Manual ", [100, 80, 600, 140], 1),
                block("paragraph_title", "Installation ", [100, 200, 400, 240], 2, title_level=1),
                block("text", "Mount the pump on a level surface.", [100, 280, 1100, 320], 3),
                block("text", "Close the valve.", [120, 340, 600, 370], 4),
                block("text", "   ", [100, 400, 200, 410], 5),  # blank: dropped
            ],
        },
        {
            "page": 2,
            "width": 1200,
            "height": 1600,
            "blocks": [
                block("paragraph_title", "Specifications", [100, 80, 500, 120], 1, title_level=2),
                block("table", TABLE_HTML, [100, 160, 600, 400], None),
                block("image", "", [100, 440, 600, 900], None),
                block("figure_title", "Figure 1: exploded view", [100, 920, 600, 960], 2),
            ],
        },
    ]


def test_mapping() -> None:
    ir = to_ir(sample(), source_name="manual.pdf")
    assert ir["schema_version"] == "1.0"
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
        ("paragraph", None, "Close the valve."),
        ("heading", 3, "Specifications"),
    ]
    table = next(b for b in ir["blocks"] if b["type"] == "table")
    assert table["text"] == "| Flow |\n| --- |\n| 40 l/min |"
    assert table["html"] == "<table><tr><td>Flow</td></tr><tr><td>40 l/min</td></tr></table>"
    figure = next(b for b in ir["blocks"] if b["type"] == "figure")
    caption = next(b for b in ir["blocks"] if b["type"] == "caption")
    assert figure["text"] == "" and figure["page"] == 2
    assert caption["text"] == "Figure 1: exploded view" and caption["page"] == 2
    assert [b["id"] for b in ir["blocks"]] == [f"b{i}" for i in range(1, len(ir["blocks"]) + 1)]
    assert all(b["text"].strip() or b["type"] == "figure" for b in ir["blocks"])


def test_bbox_units() -> None:
    """Boxes are the engine's pixels and so are the page sizes: nothing is scaled."""
    ir = to_ir(sample())
    first = ir["blocks"][0]
    assert first["bbox"] == [100.0, 80.0, 600.0, 140.0] and first["page"] == 1
    for b in ir["blocks"]:
        page = next(p for p in ir["pages"] if p["page"] == b["page"])
        x0, y0, x1, y1 = b["bbox"]
        assert 0 <= x0 < x1 <= page["width"] and 0 <= y0 < y1 <= page["height"]


def test_block_without_bbox_has_none() -> None:
    pages = [{"page": 1, "width": 10, "height": 10, "blocks": [{"label": "text", "content": "x", "bbox": None}]}]
    assert "bbox" not in to_ir(pages)["blocks"][0]


@pytest.mark.parametrize(
    ("label", "content", "expected"),
    [
        ("doc_title", "T", {"type": "heading", "level": 1, "text": "T"}),
        ("abstract_title", "A", {"type": "heading", "level": 2, "text": "A"}),
        ("reference_title", "R", {"type": "heading", "level": 2, "text": "R"}),
        ("content_title", "C", {"type": "heading", "level": 2, "text": "C"}),
        ("text", " t ", {"type": "paragraph", "text": "t"}),
        ("abstract", "a", {"type": "paragraph", "text": "a"}),
        ("content", "c", {"type": "paragraph", "text": "c"}),
        ("reference", "r", {"type": "paragraph", "text": "r"}),
        ("formula", "E=mc^2", {"type": "formula", "text": "E=mc^2"}),
        ("algorithm", "for i in x:\n  f(i)", {"type": "code", "text": "for i in x:\n  f(i)"}),
        ("figure", "", {"type": "figure", "text": ""}),
        ("chart", "a chart", {"type": "figure", "text": "a chart"}),
        ("flowchart", "", {"type": "figure", "text": ""}),
        ("seal", "ACME", {"type": "figure", "text": "ACME"}),
        ("table_title", "Table 1", {"type": "caption", "text": "Table 1"}),
        ("chart_title", "Chart", {"type": "caption", "text": "Chart"}),
        ("figure_title", "Fig", {"type": "caption", "text": "Fig"}),
        ("figure_table_chart_title", "FTC", {"type": "caption", "text": "FTC"}),
        ("footnote", "1 Note", {"type": "footnote", "text": "1 Note"}),
        ("header", "Acme", {"type": "page_header", "text": "Acme"}),
        ("footer", "Confidential", {"type": "page_footer", "text": "Confidential"}),
        ("number", "7", {"type": "page_footer", "text": "7"}),
        ("aside_text", "margin", {"type": "aside_text", "text": "margin"}),
        ("something_new", "x", {"type": "something_new", "text": "x"}),
    ],
)
def test_label_mapping(label: str, content: str, expected: dict[str, Any]) -> None:
    pages = [
        {"page": 1, "width": 10, "height": 10, "blocks": [{"label": label, "content": content, "bbox": [1, 2, 3, 4]}]}
    ]
    (got,) = to_ir(pages)["blocks"]
    assert {k: got[k] for k in expected} == expected
    assert got["page"] == 1 and got["bbox"] == [1.0, 2.0, 3.0, 4.0]


@pytest.mark.parametrize("label", ["header_image", "footer_image", "formula_number"])
def test_dropped_labels(label: str) -> None:
    pages = [{"page": 1, "width": 10, "height": 10, "blocks": [{"label": label, "content": "x", "bbox": [0, 0, 1, 1]}]}]
    assert to_ir(pages)["blocks"] == []


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


def test_table_without_html_keeps_the_text_and_has_no_html() -> None:
    """With table recognition off, the table block's content is plain text."""
    pages = [{"page": 1, "width": 10, "height": 10, "blocks": [{"label": "table", "content": "Flow 40 l/min "}]}]
    (table,) = to_ir(pages)["blocks"]
    assert table["type"] == "table" and table["text"] == "Flow 40 l/min" and "html" not in table


def test_table_html_that_has_no_rows_falls_back_to_text() -> None:
    pages = [
        {"page": 1, "width": 10, "height": 10, "blocks": [{"label": "table", "content": "<table>just text</table>"}]}
    ]
    (table,) = to_ir(pages)["blocks"]
    assert table["text"] == "just text" and table["html"] == "<table>just text</table>"


def test_pages_are_sorted_and_ids_are_global() -> None:
    pages = sample()[::-1]
    ir = to_ir(pages)
    assert [p["page"] for p in ir["pages"]] == [1, 2]
    assert ir["blocks"][0]["text"] == "Pump Manual" and ir["blocks"][0]["id"] == "b1"


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
    def run(content: bytes, filename: str, options: dict[str, bool]) -> list[dict[str, Any]]:
        calls.append((content, filename, options))
        if filename.endswith(".bad.pdf"):
            raise RuntimeError("broken file")
        if filename.endswith(".down.pdf"):
            raise ShimError(503, "engine_unavailable", "cannot start PP-StructureV3")
        return sample()

    return run


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADDLEOCR_SHIM_API_KEY", raising=False)
    monkeypatch.delenv("PADDLEOCR_SHIM_LANG", raising=False)


def post(client: TestClient, name: str = "m.pdf", options: Any = None) -> Any:
    data = {} if options is None else {"options": options if isinstance(options, str) else json.dumps(options)}
    return client.post("/parse", files={"file": (name, b"%PDF", "application/pdf")}, data=data)


def test_service_endpoints() -> None:
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_engine(calls)))
    info = client.get("/info").json()
    assert info["protocol"] == "ragcentre.parser/v1" and info["name"] == NAME == "paddleocr-shim"
    assert info["version"].startswith("0.1.0+paddleocr.")
    assert info["mime_types"] == ["application/pdf", "image/png", "image/jpeg", "image/bmp", "image/tiff", "image/webp"]
    assert info["capabilities"] == {"page_images": False, "async": True}

    resp = post(client, options={"tables": False, "heading_levels": True})
    assert resp.status_code == 200
    ir = resp.json()
    assert ir["blocks"][0]["text"] == "Pump Manual" and ir["metadata"]["source_name"] == "m.pdf"
    defaults = {
        "tables": True,
        "formulas": True,
        "charts": False,
        "seals": False,
        "orientation": False,
        "unwarp": False,
        "textline_orientation": None,  # unset: the engine's own default
        "region_detection": None,
        "pipeline": "structure_v3",
        "lang": None,
        "text_det_limit_side_len": None,
        "list_heuristic": False,
    }
    assert calls[0] == (b"%PDF", "m.pdf", {**defaults, "tables": False, "heading_levels": True})
    assert post(client).status_code == 200
    assert calls[1][2] == {**defaults, "heading_levels": False}  # no options: the defaults

    bad = post(client, options='["not an object"]')
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_options"
    wrong_type = post(client, options={"tables": "yes"})
    assert wrong_type.status_code == 400 and "tables" in wrong_type.json()["error"]["message"]
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


def test_lang_is_part_of_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ".lang-" not in service_version()
    assert service_version("en").endswith(".lang-en")
    monkeypatch.setenv("PADDLEOCR_SHIM_LANG", "fr")
    info = TestClient(create_app(fake_engine([]))).get("/info").json()
    assert info["version"].endswith(".lang-fr") and info["version"].startswith("0.1.0+paddleocr.")


def test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADDLEOCR_SHIM_API_KEY", "s3cret")
    client = TestClient(create_app(fake_engine([])))
    assert client.get("/info").status_code == 200  # RAGCentre reads /info without credentials
    assert post(client).status_code == 401
    ok = client.post(
        "/parse", files={"file": ("m.pdf", b"x", "application/pdf")}, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


# --- engine (with a stand-in for the paddleocr package) ------------------------------------------


class FakeResult(dict[str, Any]):
    pass


def fake_result(page_index: int | None, blocks: list[types.SimpleNamespace]) -> FakeResult:
    return FakeResult(page_index=page_index, page_count=None if page_index is None else 2, width=1224, height=1584,
                      parsing_res_list=blocks)  # fmt: skip


def layout_block(label: str, content: str, bbox: list[int], order: int | None, **extra: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(label=label, content=content, bbox=bbox, order_index=order, **extra)


def test_reduce_page() -> None:
    result = fake_result(
        0,
        [
            layout_block("paragraph_title", "T", [1, 2, 3, 4], 1, title_level=2),
            layout_block("table", "<table/>", [5, 6, 7, 8], None),
        ],
    )
    assert reduce_page(result, 1) == {
        "page": 1,
        "width": 1224,
        "height": 1584,
        "blocks": [
            {"label": "paragraph_title", "content": "T", "bbox": [1, 2, 3, 4], "order": 1, "title_level": 2},
            {"label": "table", "content": "<table/>", "bbox": [5, 6, 7, 8], "order": None, "title_level": None},
        ],
    }


def test_reduce_page_uses_the_position_when_page_index_is_none() -> None:
    """A single image has ``page_index`` None (confirmed against PaddleX 3.7.2)."""
    assert reduce_page(fake_result(None, []), 1)["page"] == 1
    assert reduce_page(fake_result(None, []), 3)["page"] == 3
    assert reduce_page(fake_result(1, []), 1)["page"] == 2  # page_index is 0-based for PDFs


class FakePipeline:
    instances: ClassVar[list[FakePipeline]] = []

    def __init__(self, **kwargs: Any) -> None:
        if kwargs.get("lang") == "boom":
            raise ValueError("no models for this language")
        self.kwargs = kwargs
        self.predict_calls: list[tuple[Path, dict[str, Any], bytes]] = []
        FakePipeline.instances.append(self)

    def predict(self, path: str, **kwargs: Any) -> Any:
        self.predict_calls.append((Path(path), kwargs, Path(path).read_bytes()))
        yield fake_result(0, [layout_block("text", "hello", [1, 2, 3, 4], 1)])


@pytest.fixture
def fake_paddleocr(monkeypatch: pytest.MonkeyPatch) -> type[FakePipeline]:
    FakePipeline.instances = []
    module = types.ModuleType("paddleocr")
    module.PPStructureV3 = FakePipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    return FakePipeline


OPTIONS = {"tables": True, "formulas": True, "charts": False, "seals": False, "orientation": False, "unwarp": False}
FLAGS = {
    "use_table_recognition": True,
    "use_formula_recognition": True,
    "use_chart_recognition": False,
    "use_seal_recognition": False,
    "use_doc_orientation_classify": False,
    "use_doc_unwarping": False,
}


def test_engine_passes_options_to_the_constructor_and_predict(fake_paddleocr: type[FakePipeline]) -> None:
    engine = PaddleEngine()
    pages = engine(b"%PDF-data", "m.PDF", {**OPTIONS, "seals": True, "orientation": True})
    (pipeline,) = fake_paddleocr.instances
    expected = {**FLAGS, "use_seal_recognition": True, "use_doc_orientation_classify": True}
    assert pipeline.kwargs == expected  # features must be on when the pipeline is built
    (path, kwargs, content) = pipeline.predict_calls[0]
    assert kwargs == expected and content == b"%PDF-data" and path.suffix == ".pdf"
    assert not path.exists()  # the temporary file is removed
    assert pages == [
        {
            "page": 1,
            "width": 1224,
            "height": 1584,
            "blocks": [{"label": "text", "content": "hello", "bbox": [1, 2, 3, 4], "order": 1, "title_level": None}],
        }
    ]


def test_engine_reuses_current_pipeline_when_model_options_match(fake_paddleocr: type[FakePipeline]) -> None:
    engine = PaddleEngine()
    engine(b"x", "a.pdf", OPTIONS)
    engine(b"x", "b.pdf", OPTIONS)
    assert len(fake_paddleocr.instances) == 1 and len(fake_paddleocr.instances[0].predict_calls) == 2
    engine(b"x", "c.pdf", {**OPTIONS, "charts": True})
    assert len(fake_paddleocr.instances) == 2
    engine(b"x", "d.pdf", {**OPTIONS, "charts": True, "heading_levels": True})  # heading levels reuse it
    assert len(fake_paddleocr.instances) == 2


def test_engine_passes_lang_to_the_constructor(fake_paddleocr: type[FakePipeline]) -> None:
    PaddleEngine(lang="en")(b"x", "a.pdf", OPTIONS)
    assert fake_paddleocr.instances[0].kwargs["lang"] == "en"
    PaddleEngine()(b"x", "a.pdf", OPTIONS)
    assert "lang" not in fake_paddleocr.instances[1].kwargs


def test_engine_start_failure_is_503(fake_paddleocr: type[FakePipeline]) -> None:
    with pytest.raises(ShimError) as err:
        PaddleEngine(lang="boom")(b"x", "a.pdf", OPTIONS)
    assert err.value.status == 503 and err.value.code == "engine_unavailable" and "no models" in err.value.message


def test_engine_missing_paddleocr_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "paddleocr", None)  # makes ``import paddleocr`` raise ImportError
    with pytest.raises(ShimError) as err:
        PaddleEngine()(b"x", "a.pdf", OPTIONS)
    assert err.value.status == 503


def test_engine_predict_failure_propagates_for_a_422(
    fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(self: FakePipeline, path: str, **kwargs: Any) -> Any:
        raise RuntimeError("bad page")

    monkeypatch.setattr(FakePipeline, "predict", explode)
    client = TestClient(create_app(PaddleEngine()))
    resp = post(client)
    assert resp.status_code == 422 and "bad page" in resp.json()["error"]["message"]


def test_heading_levels_option_runs_the_levelling_after_predict(
    fake_paddleocr: type[FakePipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[list[Any]]] = []

    def assign(blocks_by_page: list[list[Any]]) -> None:
        seen.append(blocks_by_page)
        blocks_by_page[0][0].title_level = 2

    for name in (
        "paddlex",
        "paddlex.inference",
        "paddlex.inference.pipelines",
        "paddlex.inference.pipelines.layout_parsing",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    leveller = types.ModuleType("paddlex.inference.pipelines.layout_parsing.title_level")
    leveller.assign_levels_to_parsing_res = assign  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, leveller.__name__, leveller)

    class Titled(FakePipeline):
        def predict(self, path: str, **kwargs: Any) -> Any:
            yield fake_result(0, [layout_block("paragraph_title", "Installation", [1, 2, 3, 4], 1)])

    monkeypatch.setattr(sys.modules["paddleocr"], "PPStructureV3", Titled)
    engine = PaddleEngine()
    assert engine(b"x", "a.pdf", OPTIONS)[0]["blocks"][0]["title_level"] is None and not seen
    assert engine(b"x", "a.pdf", {**OPTIONS, "heading_levels": True})[0]["blocks"][0]["title_level"] == 2


def test_heading_levels_failure_is_ignored(fake_paddleocr: type[FakePipeline]) -> None:
    # PaddleX is not importable here, so the levelling raises ImportError; the parse still succeeds.
    pages = PaddleEngine()(b"x", "a.pdf", {**OPTIONS, "heading_levels": True})
    assert pages[0]["blocks"][0]["title_level"] is None


# --- remote mode: a running PaddleX serving instance -----------------------------------------------


def serving_response(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """What PaddleX basic serving's ``POST /layout-parsing`` returns (field names from
    paddlex/inference/pipelines/layout_parsing/result_v2.py ``_to_json``). It has no ``title_level``."""
    return {
        "logId": "1",
        "errorCode": 0,
        "errorMsg": "Success",
        "result": {
            "layoutParsingResults": [
                {
                    "prunedResult": {
                        "page_index": page["page"] - 1,
                        "page_count": len(pages),
                        "width": page["width"],
                        "height": page["height"],
                        "parsing_res_list": [
                            {
                                "block_label": b["label"],
                                "block_content": b["content"],
                                "block_bbox": b["bbox"],
                                "block_id": i,
                                "block_order": b["order"],
                            }
                            for i, b in enumerate(page["blocks"])
                        ],
                    },
                    "markdown": {"text": "", "isStart": True, "isEnd": True},
                }
                for page in pages
            ],
            "dataInfo": {"width": 1200, "height": 1600, "type": "pdf"},
        },
    }


def remote_client(handler: Any) -> TestClient:
    import httpx

    from paddleocr_shim.remote import PaddleServingEngine

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return TestClient(create_app(PaddleServingEngine("http://paddlex:8080/", client=http)))


def test_remote_gives_the_same_ir_as_the_in_process_engine() -> None:
    import copy

    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=serving_response(sample()))

    resp = post(remote_client(handler), options={"tables": False, "charts": True})
    assert resp.status_code == 200
    flat = copy.deepcopy(sample())  # the server's JSON has no title_level
    for page in flat:
        for b in page["blocks"]:
            b["title_level"] = None
    assert resp.json() == to_ir(flat, source_name="m.pdf")
    request = seen[0]
    sent = json.loads(request.content)
    assert request.url.path == "/layout-parsing" and sent["fileType"] == 0 and sent["useTableRecognition"] is False
    assert sent["useChartRecognition"] is True and sent["useFormulaRecognition"] is True
    import base64

    assert base64.b64decode(sent["file"]) == b"%PDF"


def test_remote_image_and_version() -> None:
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=serving_response(sample()[:1]))

    client = remote_client(handler)
    resp = client.post("/parse", files={"file": ("scan.png", b"png", "image/png")})
    assert resp.status_code == 200 and json.loads(seen[0].content)["fileType"] == 1
    assert client.get("/info").json()["version"] == f"{SHIM_VERSION}+paddlex-serving.remote"


def test_remote_null_page_index_uses_the_position() -> None:
    from paddleocr_shim.remote import reduce_pruned

    page = reduce_pruned({"page_index": None, "width": 10, "height": 20, "parsing_res_list": []}, 3)
    assert page == {"page": 3, "width": 10, "height": 20, "blocks": []}


@pytest.mark.parametrize(("status", "expected"), [(422, 422), (400, 422), (401, 502), (500, 503), (503, 503)])
def test_remote_errors_are_classified(status: int, expected: int) -> None:
    import httpx

    body = {"logId": "1", "errorCode": status, "errorMsg": "nope"}
    resp = post(remote_client(lambda request: httpx.Response(status, json=body)))
    assert resp.status_code == expected and "nope" in resp.json()["error"]["message"]


def test_remote_unreachable_and_malformed() -> None:
    import httpx

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert post(remote_client(refuse)).json()["error"]["code"] == "upstream_unreachable"
    odd = post(remote_client(lambda request: httpx.Response(200, json={"result": {}})))
    assert odd.status_code == 502 and odd.json()["error"]["code"] == "upstream_invalid"


def test_remote_is_chosen_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from paddleocr_shim.remote import PaddleServingEngine

    monkeypatch.delenv("PADDLEX_SERVING_URL", raising=False)
    assert PaddleServingEngine.from_env() is None
    monkeypatch.setenv("PADDLEX_SERVING_URL", "http://paddlex:8080/")
    monkeypatch.setenv("PADDLEX_SERVING_API_KEY", "k")
    engine = PaddleServingEngine.from_env()
    assert engine is not None and engine.url == "http://paddlex:8080"
    assert engine.client.headers["authorization"] == "Bearer k"


def test_options_are_published_and_typos_are_rejected() -> None:
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_engine(calls)))
    schema = client.get("/info").json()["options_schema"]
    assert schema["type"] == "object" and schema["additionalProperties"] is False and schema["properties"]
    assert all("description" in spec for spec in schema["properties"].values())
    bad = post(client, options={"definitely_not_an_option": True})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_options"
    assert "definitely_not_an_option" in bad.json()["error"]["message"] and calls == []


# --- more engine options, PaddleOCR-VL and the list heuristic ---------------------------------------


def test_unset_flags_are_not_passed_and_set_ones_are(fake_paddleocr: type[FakePipeline]) -> None:
    PaddleEngine()(b"x", "a.pdf", {**OPTIONS, "textline_orientation": None, "region_detection": None})
    assert "use_textline_orientation" not in fake_paddleocr.instances[0].kwargs
    PaddleEngine()(b"x", "a.pdf", {**OPTIONS, "textline_orientation": True, "region_detection": False})
    assert fake_paddleocr.instances[1].kwargs["use_textline_orientation"] is True
    assert fake_paddleocr.instances[1].kwargs["use_region_detection"] is False
    assert fake_paddleocr.instances[1].predict_calls[0][1]["use_region_detection"] is False


def test_lang_and_detection_limit_per_request(fake_paddleocr: type[FakePipeline]) -> None:
    engine = PaddleEngine(lang="en")
    engine(b"x", "a.pdf", OPTIONS)
    engine(b"x", "b.pdf", {**OPTIONS, "lang": "japan"})
    engine(b"x", "c.pdf", {**OPTIONS, "text_det_limit_side_len": 1920})
    engine(b"x", "d.pdf", {**OPTIONS, "lang": "japan"})
    langs = [p.kwargs.get("lang") for p in fake_paddleocr.instances]
    assert langs == ["en", "japan", "en", "japan"] and len(fake_paddleocr.instances) == 4
    limited = fake_paddleocr.instances[2]
    assert limited.kwargs["text_det_limit_side_len"] == 1920
    assert limited.predict_calls[0][1]["text_det_limit_side_len"] == 1920
    assert "text_det_limit_side_len" not in fake_paddleocr.instances[0].kwargs


class FakeVlBlock:
    def __init__(self, label: str, content: str, bbox: list[int], title_level: int | None = None) -> None:
        self.label, self.content, self.bbox = label, content, bbox
        if title_level is not None:
            self.title_level = title_level


class FakeVl:
    instances: ClassVar[list[FakeVl]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.predict_kwargs: list[dict[str, Any]] = []
        self.restructure_kwargs: list[dict[str, Any]] = []
        FakeVl.instances.append(self)

    def predict(self, path: str, **kwargs: Any) -> list[Any]:
        self.predict_kwargs.append(kwargs)
        return ["raw-page-1", "raw-page-2"]

    def restructure_pages(self, results: list[Any], **kwargs: Any) -> list[Any]:
        assert results == ["raw-page-1", "raw-page-2"]
        self.restructure_kwargs.append(kwargs)
        page_one = {
            "page_index": 0, "width": [1224], "height": [1584],
            "parsing_res_list": [
                FakeVlBlock("doc_title", "Pump Manual", [1, 2, 3, 4], 0),
                FakeVlBlock("paragraph_title", "Installation", [1, 2, 3, 4], 1),
                FakeVlBlock("text", "Mount the pump.", [1, 2, 3, 4]),
                FakeVlBlock("vision_footnote", "Photo: ACME", [1, 2, 3, 4]),
            ],
        }  # fmt: skip
        page_two = {
            "page_index": None, "width": 1224, "height": 1584,
            "parsing_res_list": [FakeVlBlock("paragraph_title", "Specifications", [5, 6, 7, 8], 2)],
        }  # fmt: skip
        return [page_one, page_two]


@pytest.fixture
def fake_vl(monkeypatch: pytest.MonkeyPatch) -> type[FakeVl]:
    FakeVl.instances = []
    module = types.ModuleType("paddleocr")
    module.PaddleOCRVL = FakeVl  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    for name in ("VERSION", "BACKEND", "SERVER_URL", "API_MODEL", "API_KEY"):
        monkeypatch.delenv(f"PADDLEOCR_VL_{name}", raising=False)
    return FakeVl


VL = {**OPTIONS, "pipeline": "vl"}


def test_vl_pipeline_reads_levels_and_pages(fake_vl: type[FakeVl]) -> None:
    pages = PaddleEngine()(b"%PDF", "m.pdf", {**VL, "tables": False, "orientation": True})
    (vl,) = fake_vl.instances
    # VL has no table or formula switches; the others go to the constructor and to predict.
    assert vl.kwargs == {"use_chart_recognition": False, "use_seal_recognition": False,
                         "use_doc_orientation_classify": True, "use_doc_unwarping": False}  # fmt: skip
    assert vl.predict_kwargs == [vl.kwargs]
    assert vl.restructure_kwargs == [{"merge_tables": True, "relevel_titles": True}]
    assert [p["page"] for p in pages] == [1, 2] and pages[0]["width"] == 1224
    assert [(b["label"], b["title_level"], b["order"]) for b in pages[0]["blocks"]][:2] == [
        ("doc_title", 0, 1),
        ("paragraph_title", 1, 2),
    ]
    ir = to_ir(pages, source_name="m.pdf")
    assert [(b["type"], b.get("level")) for b in ir["blocks"] if b["type"] == "heading"] == [
        ("heading", 1),
        ("heading", 2),
        ("heading", 3),
    ]
    assert [b["type"] for b in ir["blocks"]][-2:] == ["footnote", "heading"]


def test_vl_settings_come_from_the_environment(fake_vl: type[FakeVl], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADDLEOCR_VL_BACKEND", "vllm-server")
    monkeypatch.setenv("PADDLEOCR_VL_SERVER_URL", "http://vllm:8118/v1")
    monkeypatch.setenv("PADDLEOCR_VL_VERSION", "v1.5")
    PaddleEngine()(b"x", "a.pdf", VL)
    assert fake_vl.instances[0].kwargs["vl_rec_backend"] == "vllm-server"
    assert fake_vl.instances[0].kwargs["vl_rec_server_url"] == "http://vllm:8118/v1"
    assert fake_vl.instances[0].kwargs["pipeline_version"] == "v1.5"
    assert service_version().endswith(".vl-v1.5.vl-vllm-server")  # the VL model and backend are part of the version


def test_vl_pipelines_are_cached_and_start_failures_are_503(
    fake_vl: type[FakeVl], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = PaddleEngine()
    engine(b"x", "a.pdf", VL)
    engine(b"x", "b.pdf", {**VL, "tables": False})  # tables is not a VL flag: same pipeline
    assert len(fake_vl.instances) == 1
    engine(b"x", "c.pdf", {**VL, "charts": True})
    assert len(fake_vl.instances) == 2
    monkeypatch.setitem(sys.modules, "paddleocr", None)
    with pytest.raises(ShimError) as err:
        PaddleEngine()(b"x", "a.pdf", VL)
    assert err.value.status == 503 and "PaddleOCR-VL" in err.value.message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\u2022 first\n\u2022 second", [("list_item", "first"), ("list_item", "second")]),
        ("- a\n- b", [("list_item", "a"), ("list_item", "b")]),
        (
            "1. one\n2) two\n(3) three\na) four",
            [("list_item", t) for t in ("1. one", "2) two", "(3) three", "a) four")],
        ),
        ("Just a sentence.", [("paragraph", "Just a sentence.")]),
        ("A. Introduction", [("paragraph", "A. Introduction")]),  # capitals are not list markers
        ("1.5 million units", [("paragraph", "1.5 million units")]),
        ("- one\nplain line", [("paragraph", "- one\nplain line")]),  # all lines or none
        ("-5 degrees", [("paragraph", "-5 degrees")]),
    ],
)
def test_list_heuristic(text: str, expected: list[tuple[str, str]]) -> None:
    pages = [{"page": 1, "width": 10, "height": 10, "blocks": [block("text", text, [0, 0, 1, 1])]}]
    plain = to_ir(pages)["blocks"]
    assert [(b["type"], b["text"]) for b in plain] == [("paragraph", text)]  # off by default
    guessed = to_ir(pages, list_heuristic=True)["blocks"]
    assert [(b["type"], b["text"]) for b in guessed] == expected
    assert all(b["page"] == 1 and b["bbox"] == [0.0, 0.0, 1.0, 1.0] for b in guessed)


def test_remote_sends_the_extra_options() -> None:
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=serving_response(sample()[:1]))

    resp = post(remote_client(handler), options={"textline_orientation": True, "text_det_limit_side_len": 2000})
    assert resp.status_code == 200
    sent = json.loads(seen[0].content)
    assert sent["useTextlineOrientation"] is True and sent["textDetLimitSideLen"] == 2000
    assert "useRegionDetection" not in sent  # unset: the server's default
