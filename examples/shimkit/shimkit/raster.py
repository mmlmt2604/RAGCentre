"""Page images for engines that read images only (EasyOCR, docTR): render a PDF's pages, or open
an image's frames, as RGB Pillow images.

Needs ``pypdfium2`` (for PDFs) and ``Pillow``, imported on first use: shimkit itself does not depend
on them, so a shim that uses this module lists them in its own dependencies.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Generator, Sequence
from typing import Any

from shimkit.service import ShimError, file_extension


def _numbers(total: int, page_range: Sequence[int] | None) -> list[int]:
    if total < 1:
        raise ShimError(422, "conversion_failed", "the document has no pages")
    if not page_range:
        return list(range(1, total + 1))
    first, last = int(page_range[0]), int(page_range[-1])
    if first > total:
        raise ShimError(400, "invalid_options", f"page_range starts after the last page ({total})")
    return list(range(first, min(last, total) + 1))


def _pdf(content: bytes, dpi: int, page_range: Sequence[int] | None) -> Generator[tuple[int, Any]]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(content)
    try:
        for number in _numbers(len(document), page_range):
            page = document[number - 1]
            try:
                bitmap = page.render(scale=dpi / 72)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
            yield number, image
    finally:
        document.close()


def _image(content: bytes, page_range: Sequence[int] | None) -> Generator[tuple[int, Any]]:
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(content)) as image:
        for number in _numbers(getattr(image, "n_frames", 1), page_range):
            image.seek(number - 1)
            # A phone photo is often stored sideways with an EXIF orientation tag: turn it upright.
            yield number, ImageOps.exif_transpose(image).convert("RGB")


def iter_pages(
    content: bytes, filename: str, *, dpi: int, page_range: Sequence[int] | None = None
) -> Generator[tuple[int, Any]]:
    """Yield one ``(page number, RGB PIL image)`` at a time without rendering ahead.

    The caller owns each yielded image and should close it after use. Use
    ``contextlib.closing(iter_pages(...))`` when iteration may stop early, so the
    underlying PDF or image file closes even if OCR fails.
    """
    if file_extension(filename) == "pdf":
        yield from _pdf(content, dpi, page_range)
    else:
        yield from _image(content, page_range)


def render_pages(
    content: bytes, filename: str, *, dpi: int, page_range: Sequence[int] | None = None
) -> list[tuple[int, Any]]:
    """``[(page number, RGB PIL image)]`` for an upload: a PDF's pages rendered at ``dpi``, or the
    frames of an image (a multi-page TIFF has several, any other image one).

    ``page_range`` (``[first, last]``, 1-based and inclusive) keeps only those pages; the numbers
    stay the original's, and a range that runs past the end stops at the last page. One that starts
    after the last page is a ``400 invalid_options``. A file that cannot be read raises the
    library's own error (a ``422 conversion_failed`` from the service)."""
    return list(iter_pages(content, filename, dpi=dpi, page_range=page_range))


def encode_png(image: Any) -> dict[str, str]:
    """A PIL image as the IR's ``pages[].image``: ``{"mime": "image/png", "data_b64": ...}``."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return {"mime": "image/png", "data_b64": base64.b64encode(buffer.getvalue()).decode("ascii")}
