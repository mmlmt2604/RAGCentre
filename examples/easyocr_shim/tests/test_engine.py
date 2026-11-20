"""The EasyOCR engine with a stand-in for the ``easyocr`` package. Pages are really rendered, so
these tests need Pillow, pypdfium2 and numpy (they are skipped without them), but not EasyOCR."""

from __future__ import annotations

import io
import sys
import types
from collections.abc import Generator
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from shimkit import ShimError

from easyocr_shim.app import create_app
from easyocr_shim.engine import EasyOcrEngine, default_languages, reduce_page, to_bbox, use_gpu

Image = pytest.importorskip("PIL.Image")
pytest.importorskip("pypdfium2")
pytest.importorskip("numpy")


class FakeReader:
    instances: ClassVar[list[FakeReader]] = []

    def __init__(self, languages: list[str], gpu: bool = True, verbose: bool = True) -> None:
        if "xx" in languages:
            raise ValueError("xx is not supported")
        if "boom" in languages:
            raise OSError("model download failed")
        self.languages, self.gpu = languages, gpu
        self.calls: list[tuple[tuple[int, ...], dict[str, Any]]] = []
        FakeReader.instances.append(self)

    def readtext(self, image: Any, **kwargs: Any) -> list[Any]:
        self.calls.append((image.shape, kwargs))
        return [([[10, 20], [110, 20], [110, 44], [10, 44]], "Hello", 0.91)]


@pytest.fixture
def fake_easyocr(monkeypatch: pytest.MonkeyPatch) -> type[FakeReader]:
    FakeReader.instances = []
    module = types.ModuleType("easyocr")
    module.Reader = FakeReader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "easyocr", module)
    for name in ("EASYOCR_SHIM_LANGS", "EASYOCR_SHIM_GPU", "EASYOCR_SHIM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return FakeReader


OPTIONS = {
    "languages": None,
    "dpi": 72,
    "page_range": None,
    "decoder": "greedy",
    "rotated_text": False,
    "canvas_size": 2560,
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


def test_reads_an_image(fake_easyocr: type[FakeReader]) -> None:
    pages = EasyOcrEngine()(png(), "scan.png", OPTIONS)
    (reader,) = fake_easyocr.instances
    assert reader.languages == ["en"] and reader.gpu is True
    ((shape, kwargs),) = reader.calls
    assert shape == (200, 300, 3)  # an RGB array, height first
    assert kwargs == {"detail": 1, "paragraph": False, "decoder": "greedy", "rotation_info": None, "canvas_size": 2560}
    assert pages == [
        {
            "page": 1,
            "width": 300,
            "height": 200,
            "lines": [{"text": "Hello", "bbox": [10.0, 20.0, 110.0, 44.0], "confidence": 0.91}],
            "image": None,
        }
    ]


def test_reads_pdf_pages_in_the_range(fake_easyocr: type[FakeReader]) -> None:
    options = {**OPTIONS, "page_range": [2, 3], "rotated_text": True, "decoder": "beamsearch", "dpi": 144}
    pages = EasyOcrEngine()(pdf(3), "m.pdf", options)
    assert [p["page"] for p in pages] == [2, 3]
    assert (pages[0]["width"], pages[0]["height"]) == (1224, 1584)
    shape, kwargs = fake_easyocr.instances[0].calls[0]
    assert shape == (1584, 1224, 3)
    assert kwargs["rotation_info"] == [90, 180, 270] and kwargs["decoder"] == "beamsearch"


def test_page_images(fake_easyocr: type[FakeReader]) -> None:
    (page,) = EasyOcrEngine()(png(), "scan.png", {**OPTIONS, "include_page_images": True})
    assert page["image"]["mime"] == "image/png" and page["image"]["data_b64"]


def test_one_reader_per_language_list(fake_easyocr: type[FakeReader]) -> None:
    engine = EasyOcrEngine(languages=["en"])
    engine(png(), "a.png", OPTIONS)
    engine(png(), "b.png", OPTIONS)
    engine(png(), "c.png", {**OPTIONS, "languages": ["de", "en"]})
    engine(png(), "d.png", {**OPTIONS, "languages": ["de", "en"]})
    assert [r.languages for r in fake_easyocr.instances] == [["en"], ["de", "en"]]


def test_environment(fake_easyocr: type[FakeReader], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EASYOCR_SHIM_LANGS", " fr , de ,")
    monkeypatch.setenv("EASYOCR_SHIM_GPU", "0")
    assert default_languages() == ["fr", "de"] and use_gpu() is False
    EasyOcrEngine()(png(), "a.png", OPTIONS)
    assert fake_easyocr.instances[0].languages == ["fr", "de"] and fake_easyocr.instances[0].gpu is False


def test_bad_languages_are_400_and_start_failures_503(fake_easyocr: type[FakeReader]) -> None:
    with pytest.raises(ShimError) as err:
        EasyOcrEngine()(png(), "a.png", {**OPTIONS, "languages": ["xx"]})
    assert err.value.status == 400 and err.value.code == "invalid_options"
    with pytest.raises(ShimError) as err:
        EasyOcrEngine()(png(), "a.png", {**OPTIONS, "languages": ["boom"]})
    assert err.value.status == 503 and "model download failed" in err.value.message


def test_missing_easyocr_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "easyocr", None)  # makes ``import easyocr`` raise ImportError
    with pytest.raises(ShimError) as err:
        EasyOcrEngine()(png(), "a.png", OPTIONS)
    assert err.value.status == 503


def test_through_the_service(fake_easyocr: type[FakeReader]) -> None:
    client = TestClient(create_app(EasyOcrEngine()))
    resp = client.post("/parse", files={"file": ("scan.png", png(), "image/png")})
    assert resp.status_code == 200
    (block,) = resp.json()["blocks"]
    assert block == {
        "type": "paragraph",
        "text": "Hello",
        "page": 1,
        "bbox": [10.0, 20.0, 110.0, 44.0],
        "metadata": {"confidence": 0.91},
        "id": "b1",
    }
    unreadable = client.post("/parse", files={"file": ("scan.png", b"not a png", "image/png")})
    assert unreadable.status_code == 422 and unreadable.json()["error"]["code"] == "conversion_failed"
    past_the_end = client.post(
        "/parse", files={"file": ("scan.png", png(), "image/png")}, data={"options": '{"page_range": [2, 2]}'}
    )
    assert past_the_end.status_code == 400


@pytest.mark.parametrize("fail", [False, True])
def test_pages_are_processed_and_released_one_at_a_time(
    fake_easyocr: type[FakeReader], monkeypatch: pytest.MonkeyPatch, fail: bool
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

    def readtext(reader: FakeReader, image: Any, **kwargs: Any) -> list[Any]:
        assert len(images) == len(reader.calls) + 1  # no page is rendered ahead of OCR
        if fail:
            raise RuntimeError("OCR failed")
        reader.calls.append((image.shape, kwargs))
        return []

    monkeypatch.setattr("easyocr_shim.engine.iter_pages", pages)
    monkeypatch.setattr(FakeReader, "readtext", readtext)
    if fail:
        with pytest.raises(RuntimeError, match="OCR failed"):
            EasyOcrEngine()(b"pdf", "scan.pdf", OPTIONS)
        assert len(images) == 1
    else:
        assert len(EasyOcrEngine()(b"pdf", "scan.pdf", OPTIONS)) == 3
    assert closed == [True]
    for image in images:
        with pytest.raises(ValueError, match="closed image"):
            image.getpixel((0, 0))


def test_to_bbox_and_reduce_page_take_rotated_boxes() -> None:
    assert to_bbox([[5, 0], [10, 5], [5, 10], [0, 5]]) == [0.0, 0.0, 10.0, 10.0]
    image = types.SimpleNamespace(width=10, height=20)
    assert reduce_page([], 4, image, with_image=False) == {
        "page": 4,
        "width": 10,
        "height": 20,
        "lines": [],
        "image": None,
    }
