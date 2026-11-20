"""Calls EasyOCR and reduces what it reads to the plain line pages ``shimkit.lines_to_ir`` takes.

EasyOCR reads images, not PDFs, so PDF pages are rendered with pypdfium2 first
(``shimkit.iter_pages``). Each page goes to ``easyocr.Reader.readtext(image, detail=1,
paragraph=False)``, which returns one ``[box, text, confidence]`` per piece of text it found: ``box``
is four ``[x, y]`` corners in the image's pixels (top-left first, clockwise), and a piece is a line or
part of one (EasyOCR splits a line at a wide gap). ``paragraph=True`` would group them, but it drops
the confidences, so the shim groups the lines itself (``shimkit.layout``).

EasyOCR is imported on first use, so importing this module needs no engine. One ``Reader`` is built
per language list and kept: a reader loads the detection model and the recognition model for its
languages (downloaded into ``~/.EasyOCR`` on first use).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from contextlib import closing
from typing import Any

from shimkit import ShimError, encode_png, iter_pages

SUPPORTED_EXTENSIONS = frozenset({"pdf", "png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"})
DEFAULT_DPI = 200
DEFAULT_CANVAS_SIZE = 2560  # easyocr.Reader.readtext's own default
ROTATIONS = [90, 180, 270]


def default_languages() -> list[str]:
    """``EASYOCR_SHIM_LANGS`` (comma-separated EasyOCR codes, for example ``en,de``), else English."""
    raw = os.environ.get("EASYOCR_SHIM_LANGS", "")
    return [code.strip() for code in raw.split(",") if code.strip()] or ["en"]


def use_gpu() -> bool:
    """``EASYOCR_SHIM_GPU=0`` forces the CPU. Otherwise EasyOCR uses CUDA or Apple's MPS when
    PyTorch has them and falls back to the CPU when it does not."""
    return os.environ.get("EASYOCR_SHIM_GPU", "1").strip().lower() not in {"0", "false", "no", "off"}


def to_bbox(box: Sequence[Sequence[float]]) -> list[float]:
    """Four ``[x, y]`` corners -> ``[x0, y0, x1, y1]``: the upright rectangle around them."""
    xs = [float(point[0]) for point in box]
    ys = [float(point[1]) for point in box]
    return [min(xs), min(ys), max(xs), max(ys)]


def reduce_page(results: Sequence[Sequence[Any]], number: int, image: Any, *, with_image: bool) -> dict[str, Any]:
    """``readtext`` output for one page -> ``{"page", "width", "height", "lines", "image"}``."""
    return {
        "page": number,
        "width": image.width,
        "height": image.height,
        "lines": [
            {"text": str(text), "bbox": to_bbox(box), "confidence": float(confidence)}
            for box, text, confidence in results
        ],
        "image": encode_png(image) if with_image else None,
    }


def _array(image: Any) -> Any:
    import numpy as np  # an EasyOCR dependency

    return np.asarray(image)  # RGB, as EasyOCR's own image loader gives it


class EasyOcrEngine:
    """``(content, filename, options) -> list of line pages``. Not known to be thread-safe: the app
    serialises calls."""

    def __init__(self, languages: Sequence[str] | None = None, gpu: bool | None = None) -> None:
        self.languages = list(languages) if languages else default_languages()
        self.gpu = use_gpu() if gpu is None else gpu
        self._readers: dict[tuple[str, ...], Any] = {}

    def _reader(self, languages: Sequence[str]) -> Any:
        key = tuple(languages)
        if key not in self._readers:
            try:
                import easyocr
            except Exception as exc:
                raise ShimError(503, "engine_unavailable", f"cannot import EasyOCR: {exc}") from exc
            try:
                self._readers[key] = easyocr.Reader(list(languages), gpu=self.gpu, verbose=False)
            except ValueError as exc:
                # An unknown language code, or scripts that cannot be read together (EasyOCR raises
                # ValueError for both): the request is wrong, so retrying will not help.
                raise ShimError(400, "invalid_options", f"EasyOCR cannot read {list(languages)}: {exc}") from exc
            except Exception as exc:  # a model download failure, a broken PyTorch...
                message = f"cannot start EasyOCR: {type(exc).__name__}: {exc}"
                raise ShimError(503, "engine_unavailable", message) from exc
        return self._readers[key]

    def __call__(self, content: bytes, filename: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        reader = self._reader(options.get("languages") or self.languages)
        out = []
        with closing(iter_pages(content, filename, dpi=options["dpi"], page_range=options.get("page_range"))) as pages:
            for number, image in pages:
                try:
                    results = reader.readtext(
                        _array(image),
                        detail=1,
                        paragraph=False,
                        decoder=options["decoder"],
                        rotation_info=ROTATIONS if options["rotated_text"] else None,
                        canvas_size=options["canvas_size"],
                    )
                    out.append(reduce_page(results, number, image, with_image=options["include_page_images"]))
                finally:
                    image.close()
        return out
