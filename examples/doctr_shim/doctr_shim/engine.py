"""Calls docTR and reduces what it reads to the plain line pages ``shimkit.lines_to_ir`` takes.

Pages are rendered (PDFs, with pypdfium2) or opened (images) by ``shimkit.iter_pages`` and passed
to docTR's ``ocr_predictor`` one at a time, as RGB arrays, which is what ``DocumentFile`` would give
it. The result's ``export()`` is::

    {"pages": [{"page_idx": 0, "dimensions": [height, width],
                "blocks": [{"geometry": ..., "lines": [{"geometry": ..., "words": [
                    {"value": "Pump", "confidence": 0.99, "geometry": ...}]}]}]}]}

A ``geometry`` is relative to the page (0 to 1): ``[[x0, y0], [x1, y1]]`` for straight pages, or
four corners when ``straight_pages`` is off. The shim keeps docTR's lines (``resolve_lines``) and
leaves the blocks to ``shimkit.layout`` (``resolve_blocks`` off), so both OCR shims group paragraphs
and guess headings the same way.

docTR (and PyTorch) are imported on first use. One predictor is built per combination of models and
``straight_pages`` and kept; the models are downloaded into ``~/.cache/doctr`` on first use.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from contextlib import closing
from typing import Any

from shimkit import ShimError, encode_png, iter_pages

SUPPORTED_EXTENSIONS = frozenset({"pdf", "png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"})
DEFAULT_DPI = 144  # docTR's DocumentFile.from_pdf renders at scale 2
# doctr.models.detection / recognition architectures with pretrained weights (docTR 1.x). The first
# of each is ocr_predictor's default.
DETECTION = [
    "fast_base",
    "fast_small",
    "fast_tiny",
    "db_resnet50",
    "db_mobilenet_v3_large",
    "linknet_resnet18",
    "linknet_resnet34",
    "linknet_resnet50",
]
RECOGNITION = [
    "crnn_vgg16_bn",
    "crnn_mobilenet_v3_small",
    "crnn_mobilenet_v3_large",
    "parseq",
    "master",
    "sar_resnet31",
    "vitstr_small",
    "vitstr_base",
]


def to_bbox(geometry: Sequence[Sequence[float]], width: float, height: float) -> list[float]:
    """A relative geometry (two or four points) -> ``[x0, y0, x1, y1]`` in pixels, kept on the page."""
    xs = [min(max(float(point[0]), 0.0), 1.0) * width for point in geometry]
    ys = [min(max(float(point[1]), 0.0), 1.0) * height for point in geometry]
    return [min(xs), min(ys), max(xs), max(ys)]


def reduce_page(page: Mapping[str, Any], number: int, image: Any, *, with_image: bool) -> dict[str, Any]:
    """One page of ``result.export()`` -> ``{"page", "width", "height", "lines", "image"}``."""
    height, width = (float(v) for v in page["dimensions"])
    lines = []
    for block in page.get("blocks", []):
        for line in block.get("lines", []):
            words = [w for w in line.get("words", []) if str(w.get("value") or "").strip()]
            if not words:
                continue
            confidences = [float(w["confidence"]) for w in words if w.get("confidence") is not None]
            lines.append(
                {
                    "text": " ".join(str(w["value"]).strip() for w in words),
                    "bbox": to_bbox(line["geometry"], width, height),
                    "confidence": sum(confidences) / len(confidences) if confidences else None,
                }
            )
    return {
        "page": number,
        "width": width,
        "height": height,
        "lines": lines,
        "image": encode_png(image) if with_image else None,
    }


def resolve_device(setting: str) -> str:
    """``DOCTR_SHIM_DEVICE``: ``cpu``, ``cuda``, ``cuda:1``, ``mps``..., or ``auto`` (CUDA when
    PyTorch has it, else the CPU)."""
    if setting != "auto":
        return setting
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _array(image: Any) -> Any:
    import numpy as np  # a docTR dependency

    return np.asarray(image)


class DoctrEngine:
    """``(content, filename, options) -> list of line pages``. The app serialises calls."""

    def __init__(self, device: str | None = None) -> None:
        self.device = device or os.environ.get("DOCTR_SHIM_DEVICE", "").strip() or "auto"
        self._predictors: dict[tuple[Any, ...], Any] = {}

    def _predictor(self, options: Mapping[str, Any]) -> Any:
        key = (options["detection"], options["recognition"], options["straight_pages"])
        if key not in self._predictors:
            try:
                from doctr.models import ocr_predictor

                predictor = ocr_predictor(
                    det_arch=options["detection"],
                    reco_arch=options["recognition"],
                    pretrained=True,
                    assume_straight_pages=options["straight_pages"],
                    resolve_lines=True,
                    resolve_blocks=False,
                )
                device = resolve_device(self.device)
                if device != "cpu":
                    predictor = predictor.to(device)
                self._predictors[key] = predictor
            except Exception as exc:  # missing docTR or PyTorch, a model download failure...
                message = f"cannot start docTR: {type(exc).__name__}: {exc}"
                raise ShimError(503, "engine_unavailable", message) from exc
        return self._predictors[key]

    def __call__(self, content: bytes, filename: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        predictor = self._predictor(options)
        out = []
        with closing(iter_pages(content, filename, dpi=options["dpi"], page_range=options.get("page_range"))) as pages:
            for number, image in pages:
                try:
                    (page,) = predictor([_array(image)]).export()["pages"]
                    out.append(reduce_page(page, number, image, with_image=options["include_page_images"]))
                finally:
                    image.close()
        return out
