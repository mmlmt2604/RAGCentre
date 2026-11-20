"""Page rendering (shimkit.raster). Needs Pillow and pypdfium2; skipped without them."""

from __future__ import annotations

import base64
import io
from contextlib import closing

import pytest

from shimkit import ShimError, encode_png, iter_pages, render_pages

Image = pytest.importorskip("PIL.Image")
pytest.importorskip("pypdfium2")


def make_pdf(pages: int = 1) -> bytes:
    """A US-letter PDF with ``pages`` pages of text, assembled by hand."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages)).encode()
    objects.append(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages))
    font = 3 + 2 * pages
    for i in range(pages):
        content = b"BT /F1 24 Tf 72 700 Td (Page %d) Tj ET" % (i + 1)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (4 + 2 * i, font)
        )
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def image_bytes(fmt: str = "PNG", frames: int = 1, size: tuple[int, int] = (40, 30)) -> bytes:
    images = [Image.new("L", size, color=i * 40) for i in range(frames)]
    buffer = io.BytesIO()
    images[0].save(buffer, format=fmt, save_all=frames > 1, append_images=images[1:])
    return buffer.getvalue()


def test_pdf_pages_are_rendered_at_the_dpi() -> None:
    pages = render_pages(make_pdf(3), "m.PDF", dpi=144)
    assert [n for n, _ in pages] == [1, 2, 3]
    _, image = pages[0]
    assert image.mode == "RGB" and image.size == (1224, 1584)  # 8.5 x 11 in at 144 dpi


def test_pdf_iteration_renders_on_demand_and_closes_when_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    import pypdfium2 as pdfium

    documents = []
    rendered = []
    original_document = pdfium.PdfDocument
    original_render = pdfium.PdfPage.render

    def open_document(content):
        document = original_document(content)
        documents.append(document)
        return document

    def render(page, **kwargs):
        rendered.append(page)
        return original_render(page, **kwargs)

    monkeypatch.setattr(pdfium, "PdfDocument", open_document)
    monkeypatch.setattr(pdfium.PdfPage, "render", render)
    with closing(iter_pages(make_pdf(3), "scan.pdf", dpi=72)) as pages:
        assert not documents and not rendered
        number, image = next(pages)
        assert number == 1 and len(rendered) == 1
        assert documents[0].raw is not None
    assert documents[0].raw is None
    # The returned RGB image owns its pixels, independent of PDFium's closed bitmap/document.
    assert image.getpixel((0, 0)) == (255, 255, 255)
    image.close()


def test_tiff_iteration_keeps_frames_independent() -> None:
    with closing(iter_pages(image_bytes("TIFF", frames=3), "scan.tiff", dpi=72)) as pages:
        _, first = next(pages)
        _, second = next(pages)
    assert first.getpixel((0, 0)) == (0, 0, 0)
    assert second.getpixel((0, 0)) == (40, 40, 40)
    first.close()
    second.close()


def test_page_range_keeps_the_original_numbers_and_stops_at_the_end() -> None:
    assert [n for n, _ in render_pages(make_pdf(3), "m.pdf", dpi=72, page_range=[2, 9])] == [2, 3]
    with pytest.raises(ShimError) as err:
        render_pages(make_pdf(3), "m.pdf", dpi=72, page_range=[4, 5])
    assert err.value.status == 400 and err.value.code == "invalid_options" and "(3)" in err.value.message


def test_images_and_tiff_frames() -> None:
    ((number, image),) = render_pages(image_bytes(), "scan.png", dpi=200)
    assert number == 1 and image.mode == "RGB" and image.size == (40, 30)
    frames = render_pages(image_bytes("TIFF", frames=3), "scan.tif", dpi=200)
    assert [n for n, _ in frames] == [1, 2, 3]
    assert [n for n, _ in render_pages(image_bytes("TIFF", frames=3), "scan.tif", dpi=200, page_range=[3, 3])] == [3]


def test_exif_orientation_is_applied() -> None:
    image = Image.new("RGB", (40, 30))
    exif = image.getexif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    ((_, upright),) = render_pages(buffer.getvalue(), "photo.jpg", dpi=200)
    assert upright.size == (30, 40)


def test_unreadable_files_raise() -> None:
    with pytest.raises(Exception, match=r"(?i)cannot identify"):
        render_pages(b"not an image", "scan.png", dpi=200)
    with pytest.raises(Exception):  # noqa: B017 - pypdfium2's own error type
        render_pages(b"%PDF-1.4 broken", "m.pdf", dpi=200)


def test_encode_png() -> None:
    encoded = encode_png(Image.new("RGB", (3, 2)))
    assert encoded["mime"] == "image/png"
    assert Image.open(io.BytesIO(base64.b64decode(encoded["data_b64"]))).size == (3, 2)
