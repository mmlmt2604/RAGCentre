"""The docTR engine with a stand-in for the ``doctr`` package. Pages are really rendered, so these
tests need Pillow, pypdfium2 and numpy (they are skipped without them), but not docTR or PyTorch."""

from __future__ import annotations

import io
import sys
import types
from collections.abc import Generator
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from shimkit import ShimError

from doctr_shim.app import create_app
from doctr_shim.engine import DoctrEngine, reduce_page, resolve_device, to_bbox

Image = pytest.importorskip("PIL.Image")
pytest.importorskip("pypdfium2")
pytest.importorskip("numpy")


def word(value: str, confidence: float | None, geometry: Any) -> dict[str, Any]:
    return {"value": value, "confidence": confidence, "geometry": geometry}


def export_page(height: int, width: int) -> dict[str, Any]:
    """One page of ``Document.export()`` (docTR 1.x), with ``resolve_blocks`` off: one block."""
    return {
        "page_idx": 0,
        "dimensions": [height, width],
        "orientation": {"value": None, "confidence": None},
        "language": {"value": None, "confidence": None},
        "blocks": [
            {
                "geometry": [[0.1, 0.1], [0.9, 0.5]],
                "objectness_score": 0.9,
                "lines": [
                    {
                        "geometry": [[0.1, 0.1], [0.5, 0.2]],
                        "objectness_score": 0.9,
                        "words": [
                            word("Pump", 0.9, [[0.1, 0.1], [0.3, 0.2]]),
                            word("Manual", 0.7, [[0.3, 0.1], [0.5, 0.2]]),
                        ],
                    },
                    {"geometry": [[0.1, 0.3], [0.5, 0.4]], "words": [word(" ", 0.5, [[0.1, 0.3], [0.2, 0.4]])]},
                    {  # a rotated line (straight_pages off): four corners, one just off the page
                        "geometry": [[0.1, 0.45], [0.9, 0.42], [0.91, 0.5], [0.11, 0.53]],
                        "words": [word("tilted", None, [[0.1, 0.45], [0.9, 0.42], [0.91, 0.5], [0.11, 0.53]])],
                    },
                ],
                "artefacts": [],
            }
        ],
    }


class FakeResult:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages

    def export(self) -> dict[str, Any]:
        return {"pages": self.pages}


class FakePredictor:
    instances: ClassVar[list[FakePredictor]] = []

    def __init__(self, **kwargs: Any) -> None:
        if kwargs["det_arch"] == "boom":
            raise RuntimeError("download failed")
        self.kwargs = kwargs
        self.device = "cpu"
        self.shapes: list[tuple[int, ...]] = []
        FakePredictor.instances.append(self)

    def to(self, device: str) -> FakePredictor:
        self.device = device
        return self

    def __call__(self, pages: list[Any]) -> FakeResult:
        (page,) = pages  # the engine sends one page at a time
        self.shapes.append(page.shape)
        return FakeResult([export_page(page.shape[0], page.shape[1])])


@pytest.fixture
def fake_doctr(monkeypatch: pytest.MonkeyPatch) -> type[FakePredictor]:
    FakePredictor.instances = []
    package = types.ModuleType("doctr")
    models = types.ModuleType("doctr.models")
    models.ocr_predictor = FakePredictor  # type: ignore[attr-defined]
    package.models = models  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "doctr", package)
    monkeypatch.setitem(sys.modules, "doctr.models", models)
    monkeypatch.setitem(sys.modules, "torch", None)  # no PyTorch: "auto" means the CPU
    for name in ("DOCTR_SHIM_DEVICE", "DOCTR_SHIM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return FakePredictor


OPTIONS = {
    "detection": "fast_base",
    "recognition": "crnn_vgg16_bn",
    "straight_pages": True,
    "dpi": 72,
    "page_range": None,
    "include_page_images": False,
}


def png(size: tuple[int, int] = (300, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def pdf(pages: int) -> bytes:
    """A US-letter PDF with ``pages`` empty pages, assembled by hand."""
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages)).encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages)]
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"] * pages
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_reads_an_image(fake_doctr: type[FakePredictor]) -> None:
    (page,) = DoctrEngine()(png(), "scan.png", OPTIONS)
    (predictor,) = fake_doctr.instances
    assert predictor.kwargs == {
        "det_arch": "fast_base",
        "reco_arch": "crnn_vgg16_bn",
        "pretrained": True,
        "assume_straight_pages": True,
        "resolve_lines": True,
        "resolve_blocks": False,
    }
    assert predictor.shapes == [(200, 300, 3)] and predictor.device == "cpu"
    assert page["page"] == 1 and (page["width"], page["height"]) == (300.0, 200.0)
    assert page["lines"][0] == {"text": "Pump Manual", "bbox": [30.0, 20.0, 150.0, 40.0], "confidence": 0.8}
    assert page["lines"][1]["text"] == "tilted" and page["lines"][1]["confidence"] is None
    assert page["lines"][1]["bbox"] == pytest.approx([30.0, 84.0, 273.0, 106.0])  # the blank line is gone
    assert page["image"] is None


def test_reads_pdf_pages_in_the_range(fake_doctr: type[FakePredictor]) -> None:
    pages = DoctrEngine()(pdf(3), "m.pdf", {**OPTIONS, "page_range": [2, 5], "dpi": 144})
    assert [p["page"] for p in pages] == [2, 3]
    assert fake_doctr.instances[0].shapes == [(1584, 1224, 3)] * 2


def test_one_predictor_per_model_combination(fake_doctr: type[FakePredictor]) -> None:
    engine = DoctrEngine()
    engine(png(), "a.png", OPTIONS)
    engine(png(), "b.png", {**OPTIONS, "dpi": 200})  # not a model setting: the same predictor
    engine(png(), "c.png", {**OPTIONS, "recognition": "parseq", "straight_pages": False})
    assert len(fake_doctr.instances) == 2
    assert fake_doctr.instances[1].kwargs["reco_arch"] == "parseq"
    assert fake_doctr.instances[1].kwargs["assume_straight_pages"] is False


def test_device(fake_doctr: type[FakePredictor], monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_device("auto") == "cpu" and resolve_device("mps") == "mps"
    monkeypatch.setenv("DOCTR_SHIM_DEVICE", "cuda:1")
    DoctrEngine()(png(), "a.png", OPTIONS)
    assert fake_doctr.instances[0].device == "cuda:1"
    torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "torch", torch)
    assert resolve_device("auto") == "cuda"


def test_start_failures_are_503(fake_doctr: type[FakePredictor], monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ShimError) as err:
        DoctrEngine()(png(), "a.png", {**OPTIONS, "detection": "boom"})
    assert err.value.status == 503 and "download failed" in err.value.message
    monkeypatch.setitem(sys.modules, "doctr.models", None)
    with pytest.raises(ShimError) as err:
        DoctrEngine()(png(), "a.png", OPTIONS)
    assert err.value.status == 503 and err.value.code == "engine_unavailable"


def test_through_the_service(fake_doctr: type[FakePredictor]) -> None:
    client = TestClient(create_app(DoctrEngine()))
    resp = client.post(
        "/parse",
        files={"file": ("scan.png", png((1000, 1000)), "image/png")},
        data={"options": '{"headings": true, "include_page_images": true}'},
    )
    assert resp.status_code == 200
    ir = resp.json()
    assert [(b["type"], b["text"]) for b in ir["blocks"]] == [("paragraph", "Pump Manual"), ("paragraph", "tilted")]
    assert ir["blocks"][0]["metadata"] == {"confidence": 0.8}
    assert ir["pages"][0]["image"]["mime"] == "image/png"
    unreadable = client.post("/parse", files={"file": ("m.pdf", b"%PDF-1.4 nonsense", "application/pdf")})
    assert unreadable.status_code == 422 and unreadable.json()["error"]["code"] == "conversion_failed"


@pytest.mark.parametrize("fail", [False, True])
def test_pages_are_processed_and_released_one_at_a_time(
    fake_doctr: type[FakePredictor], monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    images = []
    closed = []

    def pages(*args: Any, **kwargs: Any) -> Generator[tuple[int, Any]]:
        try:
            for number in range(1, 4):
                if images:
                    with pytest.raises(ValueError, match="closed image"):
                        images[-1].getpixel((0, 0))
                image = Image.new("RGB", (300, 200))
                images.append(image)
                yield number, image
        finally:
            closed.append(True)

    original_call = FakePredictor.__call__

    def predict(predictor: FakePredictor, pages: list[Any]) -> FakeResult:
        assert len(images) == len(predictor.shapes) + 1  # no page is rendered ahead of OCR
        if fail:
            raise RuntimeError("OCR failed")
        return original_call(predictor, pages)

    monkeypatch.setattr("doctr_shim.engine.iter_pages", pages)
    monkeypatch.setattr(FakePredictor, "__call__", predict)
    if fail:
        with pytest.raises(RuntimeError, match="OCR failed"):
            DoctrEngine()(b"pdf", "scan.pdf", OPTIONS)
        assert len(images) == 1
    else:
        assert len(DoctrEngine()(b"pdf", "scan.pdf", OPTIONS)) == 3
    assert closed == [True]
    for image in images:
        with pytest.raises(ValueError, match="closed image"):
            image.getpixel((0, 0))


def test_reduce_page_and_to_bbox() -> None:
    assert to_bbox([[-0.1, 0.2], [1.2, 0.4]], 100, 50) == [0.0, 10.0, 100.0, 20.0]
    image = types.SimpleNamespace(width=10, height=20)
    page = {"dimensions": [20, 10], "blocks": [{"lines": [{"geometry": [[0, 0], [1, 1]], "words": []}]}]}
    assert reduce_page(page, 7, image, with_image=False) == {
        "page": 7,
        "width": 10.0,
        "height": 20.0,
        "lines": [],
        "image": None,
    }
