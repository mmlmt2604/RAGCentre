"""Tests for the MinerU shim. They need only fastapi, httpx and pytest: no MinerU, no models."""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shimkit import ShimError

from mineru_shim.app import MIME_TYPES, NAME, create_app
from mineru_shim.convert import to_ir
from mineru_shim.engine import MineruEngine, page_sizes, translate_error

GOLDEN = Path(__file__).parent / "golden" / "sample_ir.json"
TABLE_BODY = "<table><tbody><tr><td>Flow</td></tr><tr><td>40 l/min</td></tr></tbody></table>"
SIZES = {1: (600.0, 800.0), 2: (600.0, 800.0)}


def sample() -> list[dict[str, Any]]:
    """MinerU's content list for the two-page "Pump Manual", with the shapes of
    ``mineru/render/_internal/content_list/v1.py``: ``bbox`` on a 0 to 1000 scale, 0-based
    ``page_idx``, ``img_path`` on visual items."""
    return [
        {"type": "text", "text": "Pump Manual", "text_level": 1, "bbox": [100, 50, 500, 75], "page_idx": 0},
        {"type": "text", "text": "Installation", "text_level": 2, "bbox": [100, 100, 300, 125], "page_idx": 0},
        {"type": "text", "text": "Mount the pump on a level surface.", "bbox": [100, 150, 900, 175], "page_idx": 0},
        {
            "type": "list",
            "sub_type": "text",
            "list_items": ["Close the valve."],
            "bbox": [120, 200, 500, 220],
            "page_idx": 0,
        },
        {"type": "text", "text": "   ", "bbox": [100, 300, 200, 310], "page_idx": 0},  # blank: dropped
        {"type": "text", "text": "Specifications", "text_level": 3, "bbox": [100, 50, 400, 75], "page_idx": 1},
        {
            "type": "table",
            "img_path": "data:image/jpeg;base64,AAAA",
            "table_caption": [],
            "table_footnote": [],
            "table_body": TABLE_BODY,
            "bbox": [100, 100, 600, 300],
            "page_idx": 1,
        },
        {
            "type": "image",
            "img_path": "data:image/jpeg;base64,BBBB",
            "image_caption": ["Figure 1: exploded view"],
            "image_footnote": [],
            "bbox": [100, 350, 600, 700],
            "page_idx": 1,
        },
    ]


def test_mapping() -> None:
    ir = to_ir(sample(), SIZES, source_name="manual.pdf")
    assert ir["schema_version"] == "1.0"
    assert ir["metadata"] == {"page_count": 2, "title": "Pump Manual", "source_name": "manual.pdf"}
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
    assert table["text"] == "| Flow |\n| --- |\n| 40 l/min |" and table["html"] == TABLE_BODY
    figure = next(b for b in ir["blocks"] if b["type"] == "figure")
    assert figure["text"] == "Figure 1: exploded view" and figure["page"] == 2
    assert [b["id"] for b in ir["blocks"]] == [f"b{i}" for i in range(1, len(ir["blocks"]) + 1)]
    assert all(b["text"].strip() or b["type"] == "figure" for b in ir["blocks"])
    assert "img_path" not in json.dumps(ir)  # the base64 page crops stay out of the IR


def test_bbox_units() -> None:
    """MinerU's boxes are on a 0 to 1000 scale; they are scaled to the page's size, so boxes and
    ``pages[].width/height`` share a unit."""
    ir = to_ir(sample(), SIZES)
    assert ir["blocks"][0]["bbox"] == pytest.approx([60.0, 40.0, 300.0, 60.0])  # 100*600/1000, 50*800/1000, ...
    for b in ir["blocks"]:
        page = next(p for p in ir["pages"] if p["page"] == b["page"])
        x0, y0, x1, y1 = b["bbox"]
        assert 0 <= x0 < x1 <= page["width"] and 0 <= y0 < y1 <= page["height"]


def test_page_sizes_can_differ_per_page() -> None:
    ir = to_ir(sample(), {1: (600.0, 800.0), 2: (200.0, 100.0)})
    assert next(b for b in ir["blocks"] if b["text"] == "Specifications")["bbox"] == pytest.approx(
        [20.0, 5.0, 80.0, 7.5]
    )


def test_bbox_is_omitted_when_the_page_size_is_unknown() -> None:
    ir = to_ir(sample(), {})
    assert ir["pages"] == [] and "page_count" not in ir["metadata"]
    assert all("bbox" not in b and b["page"] in (1, 2) for b in ir["blocks"])
    partial = to_ir(sample(), {1: (600.0, 800.0)})
    assert all(("bbox" in b) == (b["page"] == 1) for b in partial["blocks"])


def test_item_without_bbox() -> None:
    (b,) = to_ir([{"type": "text", "text": "x", "page_idx": 0}], SIZES)["blocks"]
    assert "bbox" not in b and b["page"] == 1


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"type": "text", "text": "T", "text_level": 1}, [("heading", 1, "T")]),
        ({"type": "text", "text": "T", "text_level": 9}, [("heading", 6, "T")]),
        ({"type": "text", "text": "T", "text_level": 0}, [("paragraph", None, "T")]),
        ({"type": "text", "text": "P"}, [("paragraph", None, "P")]),
        (
            {"type": "list", "sub_type": "text", "list_items": ["a", " ", "b"]},
            [("list_item", None, "a"), ("list_item", None, "b")],
        ),
        (
            {"type": "equation", "text": "E=mc^2", "text_format": "latex", "img_path": "x"},
            [("formula", None, "E=mc^2")],
        ),
        ({"type": "equation", "img_path": "x"}, []),  # no LaTeX recognised: nothing to keep
        ({"type": "header", "text": "Acme"}, [("page_header", None, "Acme")]),
        ({"type": "footer", "text": "Confidential"}, [("page_footer", None, "Confidential")]),
        ({"type": "page_number", "text": "7"}, [("page_footer", None, "7")]),
        ({"type": "page_footnote", "text": "1 Note"}, [("footnote", None, "1 Note")]),
        ({"type": "aside_text", "text": "margin"}, [("aside_text", None, "margin")]),
        ({"type": "something_new", "text": "x"}, [("something_new", None, "x")]),
    ],
)
def test_item_mapping(item: dict[str, Any], expected: list[tuple[str, int | None, str]]) -> None:
    blocks = to_ir([{**item, "bbox": [0, 0, 500, 500], "page_idx": 0}], SIZES)["blocks"]
    assert [(b["type"], b.get("level"), b["text"]) for b in blocks] == expected
    assert all(b["page"] == 1 and b["bbox"] == [0.0, 0.0, 300.0, 400.0] for b in blocks)


def test_index_entries_lose_their_indent_marker_and_link() -> None:
    item = {"type": "index", "list_items": ["- Intro", "    - Scope", "    - [Terms](#sec-2)", "  "], "page_idx": 0}
    blocks = to_ir([item], SIZES)["blocks"]
    assert [b["text"] for b in blocks] == ["Intro", "Scope", "Terms"]
    assert all(b["type"] == "list_item" and b["metadata"] == {"original_type": "index"} for b in blocks)


def test_table_with_captions_and_footnotes() -> None:
    item = {"type": "table", "table_caption": ["Table 1: Flow", "(continued)"], "table_footnote": ["* at 20 C"],
            "table_body": "<table><tr><th colspan='2'>A</th></tr><tr><td>1</td><td>2</td></tr></table>", "page_idx": 1}  # fmt: skip
    blocks = to_ir([item], SIZES)["blocks"]
    assert [(b["type"], b["text"]) for b in blocks] == [
        ("caption", "Table 1: Flow"),
        ("caption", "(continued)"),
        ("table", "| A | A |\n| --- | --- |\n| 1 | 2 |"),
        ("footnote", "* at 20 C"),
    ]
    assert blocks[2]["html"].startswith("<table>") and all(b["page"] == 2 for b in blocks)


def test_table_without_a_body_is_dropped_but_its_captions_stay() -> None:
    item = {"type": "table", "table_caption": ["Table 2"], "table_footnote": [], "img_path": "x", "page_idx": 0}
    assert [(b["type"], b["text"]) for b in to_ir([item], SIZES)["blocks"]] == [("caption", "Table 2")]


def test_image_and_chart() -> None:
    image = {
        "type": "image",
        "image_caption": ["Fig 1", "(top view)"],
        "image_footnote": ["Source: Acme"],
        "content": "A pump.",
        "page_idx": 0,
    }
    chart = {
        "type": "chart",
        "chart_caption": ["Chart 1"],
        "chart_footnote": ["n=12"],
        "content": "Bars rise.",
        "page_idx": 0,
    }
    bare = {"type": "image", "image_caption": [], "image_footnote": [], "page_idx": 0}
    blocks = to_ir([image, chart, bare], SIZES)["blocks"]
    assert [(b["type"], b["text"]) for b in blocks] == [
        ("figure", "Fig 1\n\n(top view)\n\nA pump."),
        ("footnote", "Source: Acme"),
        ("figure", "Chart 1\n\nBars rise."),
        ("footnote", "n=12"),
        ("figure", ""),
    ]


def test_code_with_captions_and_footnotes() -> None:
    item = {
        "type": "code",
        "sub_type": "code",
        "code_body": "if x:\n    y()",
        "code_caption": ["Listing 1"],
        "code_footnote": ["note"],
        "page_idx": 0,
    }
    assert [(b["type"], b["text"]) for b in to_ir([item], SIZES)["blocks"]] == [
        ("caption", "Listing 1"),
        ("code", "if x:\n    y()"),
        ("footnote", "note"),
    ]


def test_matches_golden() -> None:
    """``tests/golden/sample_ir.json`` is this sample's IR. RAGCentre's own test suite validates the
    same file (tests/test_parser_shim_goldens.py), since the two cannot share an environment.
    Regenerate with ``UPDATE_GOLDEN=1 uv run pytest``."""
    got = to_ir(sample(), SIZES, source_name="manual.pdf")
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=2) + "\n", encoding="utf-8")
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))


# --- service ----------------------------------------------------------------------------------


def fake_engine(calls: list[tuple[Any, ...]]) -> Any:
    def run(
        content: bytes, filename: str, options: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[int, tuple[float, float]]]:
        calls.append((content, filename, options))
        if filename.startswith("bad"):
            raise RuntimeError("broken file")
        if filename.startswith("down"):
            raise ShimError(503, "engine_unavailable", "no VLM server")
        return sample(), SIZES

    return run


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINERU_SHIM_API_KEY", raising=False)


def post(client: TestClient, name: str = "m.pdf", options: Any = None) -> Any:
    data = {} if options is None else {"options": options if isinstance(options, str) else json.dumps(options)}
    return client.post("/parse", files={"file": (name, b"%PDF", "application/pdf")}, data=data)


def test_service_endpoints() -> None:
    calls: list[tuple[Any, ...]] = []
    client = TestClient(create_app(fake_engine(calls)))
    info = client.get("/info").json()
    assert info["protocol"] == "ragcentre.parser/v1" and info["name"] == NAME == "mineru-shim"
    assert info["version"].startswith("0.1.0+mineru.")
    assert "application/pdf" in info["mime_types"] and "text/html" in info["mime_types"]
    assert info["mime_types"] == MIME_TYPES and info["capabilities"] == {"page_images": False}

    resp = post(client, options={"tier": "basic", "ocr": True, "image_analysis": True})
    assert resp.status_code == 200
    assert resp.json()["blocks"][0]["text"] == "Pump Manual" and resp.json()["metadata"]["source_name"] == "m.pdf"
    assert calls[0] == (b"%PDF", "m.pdf", {"tier": "basic", "ocr_mode": "ocr", "image_analysis": True})
    assert post(client).status_code == 200
    assert calls[1][2] == {"tier": "standard", "ocr_mode": "auto", "image_analysis": False}  # defaults

    assert post(client, options="[1]").json()["error"]["code"] == "invalid_options"
    for options in ({"tier": "fast"}, {"tier": 1}, {"ocr": "yes"}, {"ocr": None}, {"ocr": 1}, {"image_analysis": "no"}):
        bad = post(client, options=options)
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_options", options
    unsupported = post(client, name="archive.zip")
    assert unsupported.status_code == 415 and unsupported.json()["error"]["code"] == "unsupported_media_type"
    assert len(calls) == 2  # the 400s and the 415 never reached the engine
    broken = post(client, name="bad.pdf")
    assert broken.status_code == 422 and "broken file" in broken.json()["error"]["message"]
    down = post(client, name="down.pdf")
    assert down.status_code == 503 and down.json()["error"]["code"] == "engine_unavailable"


@pytest.mark.parametrize(("ocr", "mode"), [(True, "ocr"), (False, "txt"), ("auto", "auto")])
def test_ocr_option_maps_to_ocr_mode(ocr: Any, mode: str) -> None:
    calls: list[tuple[Any, ...]] = []
    post(TestClient(create_app(fake_engine(calls))), options={"ocr": ocr})
    assert calls[0][2]["ocr_mode"] == mode


@pytest.mark.parametrize("name", ["a.pdf", "a.PNG", "a.jpg", "a.jpeg", "a.webp", "a.gif", "a.bmp", "a.tiff", "a.jp2"])
@pytest.mark.parametrize("tier", ["flash", "basic", "standard", "advanced"])
def test_pdfs_and_images_keep_the_requested_tier(name: str, tier: str) -> None:
    calls: list[tuple[Any, ...]] = []
    post(TestClient(create_app(fake_engine(calls))), name=name, options={"tier": tier})
    assert calls[0][2]["tier"] == tier


@pytest.mark.parametrize(
    "name",
    ["a.docx", "a.doc", "a.pptx", "a.ppt", "a.xlsx", "a.xls", "a.rtf", "a.odt", "a.ods", "a.odp", "a.html", "a.htm",
     "a.csv", "a.tsv", "a.epub", "a.ofd", "a.mhtml"],
)  # fmt: skip
def test_other_formats_are_always_flash(name: str) -> None:
    calls: list[tuple[Any, ...]] = []
    resp = post(TestClient(create_app(fake_engine(calls))), name=name, options={"tier": "advanced"})
    assert resp.status_code == 200 and calls[0][2]["tier"] == "flash"
    post(TestClient(create_app(fake_engine(calls))), name=name)  # and by default
    assert calls[1][2]["tier"] == "flash"


@pytest.mark.parametrize("name", ["a.zip", "a.txt", "a.md", "a.mobi", "a", "a.pdf.exe"])
def test_unsupported_extensions_are_415(name: str) -> None:
    assert post(TestClient(create_app(fake_engine([]))), name=name).status_code == 415


def test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_SHIM_API_KEY", "s3cret")
    client = TestClient(create_app(fake_engine([])))
    assert client.get("/info").status_code == 200  # RAGCentre reads /info without credentials
    assert post(client).status_code == 401
    ok = client.post(
        "/parse", files={"file": ("m.pdf", b"x", "application/pdf")}, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


# --- engine (with stand-ins for mineru, pypdfium2 and Pillow) -------------------------------------


class MineruError(Exception):
    """Stands in for ``mineru.errors.MineruError``: ``.code`` and ``.type``."""

    def __init__(self, code: str, type_: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.type = type_


@pytest.mark.parametrize(
    ("code", "type_", "status", "error_code"),
    [
        ("file_type_unsupported", "invalid_request_error", 415, "unsupported_media_type"),
        ("no_engine", "engine_error", 503, "engine_unavailable"),
        ("engine_unavailable", "engine_error", 503, "engine_unavailable"),
        ("rate_limit_exceeded", "rate_limit_error", 503, "engine_unavailable"),
        ("parse_timeout", "timeout_error", 503, "engine_unavailable"),
        ("file_encrypted", "invalid_request_error", 422, "conversion_failed"),
        ("file_corrupted", "invalid_request_error", 422, "conversion_failed"),
        ("page_range_invalid", "invalid_request_error", 422, "conversion_failed"),
        ("tier_unsupported_for_file_type", "invalid_request_error", 422, "conversion_failed"),
        ("something_else", "internal_error", 422, "conversion_failed"),
    ],
)
def test_error_translation(code: str, type_: str, status: int, error_code: str) -> None:
    err = translate_error(MineruError(code, type_, "details"))
    assert err is not None and (err.status, err.code) == (status, error_code)
    assert code in err.message and "details" in err.message


def test_other_exceptions_are_not_translated() -> None:
    assert translate_error(RuntimeError("x")) is None
    assert translate_error(ValueError("tier 'standard' is only supported for PDF and image files")) is None


class FakeMineru:
    def __init__(self) -> None:
        self.parse_calls: list[tuple[Path, dict[str, Any], bytes]] = []
        self.fail: Exception | None = None


@pytest.fixture
def fake_mineru(monkeypatch: pytest.MonkeyPatch) -> FakeMineru:
    state = FakeMineru()

    def parse(path: Path, **kwargs: Any) -> Any:
        state.parse_calls.append((path, kwargs, path.read_bytes()))
        if state.fail:
            raise state.fail
        return types.SimpleNamespace(middle_json="MIDDLE")

    mineru = types.ModuleType("mineru")
    mineru.parse = parse  # type: ignore[attr-defined]
    render = types.ModuleType("mineru.render")
    content_list = types.ModuleType("mineru.render.content_list")
    content_list.render_content_list = lambda middle: [{"type": "text", "text": f"from {middle}", "page_idx": 0}]  # type: ignore[attr-defined]
    for module in (mineru, render, content_list):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, "pypdfium2", None)  # no page sizes unless a test provides them
    monkeypatch.setitem(sys.modules, "PIL", None)
    return state


OPTIONS = {"tier": "basic", "ocr_mode": "auto", "image_analysis": False}


def test_engine_calls_mineru_with_a_path(fake_mineru: FakeMineru) -> None:
    content_list, sizes = MineruEngine()(b"%PDF-data", "m.PDF", {**OPTIONS, "ocr_mode": "txt", "image_analysis": True})
    ((path, kwargs, content),) = fake_mineru.parse_calls
    assert content == b"%PDF-data" and path.suffix == ".pdf" and not path.exists()
    assert kwargs == {"tier": "basic", "ocr_mode": "txt", "image_analysis": True}
    assert content_list == [{"type": "text", "text": "from MIDDLE", "page_idx": 0}] and sizes == {}


def test_engine_error_mapping(fake_mineru: FakeMineru) -> None:
    fake_mineru.fail = MineruError("engine_unavailable", "engine_error")
    with pytest.raises(ShimError) as err:
        MineruEngine()(b"x", "a.pdf", OPTIONS)
    assert err.value.status == 503
    fake_mineru.fail = MineruError("file_encrypted", "invalid_request_error")
    with pytest.raises(ShimError) as err2:
        MineruEngine()(b"x", "a.pdf", OPTIONS)
    assert err2.value.status == 422 and "file_encrypted" in err2.value.message
    fake_mineru.fail = RuntimeError("boom")  # not a MinerU error: left for the service's 422
    with pytest.raises(RuntimeError):
        MineruEngine()(b"x", "a.pdf", OPTIONS)


def test_engine_missing_mineru_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mineru", None)
    with pytest.raises(ShimError) as err:
        MineruEngine()(b"x", "a.pdf", OPTIONS)
    assert err.value.status == 503


def test_engine_end_to_end_through_the_service(fake_mineru: FakeMineru) -> None:
    resp = post(TestClient(create_app(MineruEngine())), name="notes.docx", options={"tier": "advanced"})
    assert resp.status_code == 200
    assert fake_mineru.parse_calls[0][1]["tier"] == "flash"
    assert resp.json()["blocks"] == [{"type": "paragraph", "text": "from MIDDLE", "page": 1, "id": "b1"}]


def test_page_sizes_of_a_pdf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Page:
        def __init__(self, size: tuple[float, float]) -> None:
            self._size = size

        def get_size(self) -> tuple[float, float]:
            return self._size

    class Document:
        closed = False

        def __init__(self, path: str) -> None:
            self.pages = [Page((612.0, 792.0)), Page((595.0, 842.0))]

        def __iter__(self) -> Any:
            return iter(self.pages)

        def close(self) -> None:
            Document.closed = True

    module = types.ModuleType("pypdfium2")
    module.PdfDocument = Document  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypdfium2", module)
    assert page_sizes(tmp_path / "a.pdf", "pdf") == {1: (612.0, 792.0), 2: (595.0, 842.0)}
    assert Document.closed


def test_page_sizes_of_an_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Image:
        width, height = 800, 600

        def __enter__(self) -> Image:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    pil = types.ModuleType("PIL")
    image_module = types.ModuleType("PIL.Image")
    image_module.open = lambda path: Image()  # type: ignore[attr-defined]
    pil.Image = image_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_module)
    assert page_sizes(tmp_path / "a.png", "png") == {1: (800.0, 600.0)}


def test_page_sizes_are_empty_for_other_formats_and_unreadable_files(fake_mineru: FakeMineru, tmp_path: Path) -> None:
    assert page_sizes(tmp_path / "a.docx", "docx") == {}
    assert page_sizes(tmp_path / "a.html", "html") == {}
    assert page_sizes(tmp_path / "a.pdf", "pdf") == {}  # pypdfium2 is not importable here
