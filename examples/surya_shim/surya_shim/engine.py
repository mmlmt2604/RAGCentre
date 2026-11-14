"""Calls Surya's recognition model and reduces each page result to plain dicts for ``convert``.

Surya is imported on first use, so importing this module needs no engine. One
``SuryaInferenceManager`` (which starts, or attaches to, the inference server) and one
``RecognitionPredictor`` are created on the first request and kept.
"""

from __future__ import annotations

import base64
import io
import threading
from typing import Any

from shimkit import ShimError, file_extension, temp_upload

IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"})
SUPPORTED_EXTENSIONS = frozenset({"pdf"}) | IMAGE_EXTENSIONS
DEFAULT_DPI = 192  # surya.settings.settings.IMAGE_DPI_HIGHRES

# Failures of the inference server (surya/inference/backends use the ``openai`` client and
# ``SpawnError``); these mean "try again later", not "this document is bad".
_UNAVAILABLE = {"APIConnectionError", "APITimeoutError", "SpawnError"}


def _is_unavailable(exc: BaseException) -> bool:
    return isinstance(exc, ConnectionError | TimeoutError) or type(exc).__name__ in _UNAVAILABLE


def encode_png(image: Any) -> dict[str, str]:
    """A PIL image as the IR's ``{"mime", "data_b64"}``: PNG, so it matches the boxes exactly."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return {"mime": "image/png", "data_b64": base64.b64encode(buffer.getvalue()).decode("ascii")}


def reduce_page(prediction: Any, number: int, image: Any, *, with_image: bool) -> dict[str, Any]:
    """One ``PageOCRResult`` -> ``{"page", "width", "height", "blocks", "image"}``."""
    x0, y0, x1, y1 = prediction.image_bbox
    return {
        "page": number,
        "width": x1 - x0,
        "height": y1 - y0,
        "blocks": [
            {
                "label": block.label,
                "html": block.html,
                "bbox": list(block.bbox),
                "reading_order": block.reading_order,
                "skipped": block.skipped,
                "error": block.error,
            }
            for block in prediction.blocks
        ],
        "image": encode_png(image) if with_image else None,
    }


class SuryaEngine:
    """``(content, filename, options) -> list of reduced pages``. Safe to call from several
    threads: the predictor is a client of the inference server."""

    def __init__(self) -> None:
        self._manager: Any = None
        self._predictor: Any = None
        self._lock = threading.Lock()

    def close(self) -> None:
        """Stop the inference server if this process started it. Surya registers its own ``atexit``
        cleanup, but a server that is stopped by a signal can leave ``llama-server`` running."""
        with self._lock:
            manager, self._manager, self._predictor = self._manager, None, None
        if manager is not None:
            try:
                manager.stop()
            except Exception:  # shutting down: nothing useful to do with a failure
                return

    def _get_predictor(self) -> Any:
        with self._lock:
            if self._predictor is None:
                try:
                    from surya import RecognitionPredictor, SuryaInferenceManager

                    self._manager = SuryaInferenceManager()
                    self._predictor = RecognitionPredictor(self._manager)
                except Exception as exc:
                    message = f"cannot start the Surya inference server: {type(exc).__name__}: {exc}"
                    raise ShimError(503, "engine_unavailable", message) from exc
            return self._predictor

    def __call__(self, content: bytes, filename: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        predictor = self._get_predictor()
        from surya.input.load import load_image, load_pdf

        with temp_upload(content, filename) as path:
            if file_extension(filename) == "pdf":
                images, _ = load_pdf(path, dpi=options["dpi"])
            else:
                images, _ = load_image(path)
        try:
            predictions = predictor(images, skip_labels=[] if options["ocr_figures"] else None)
        except Exception as exc:
            if _is_unavailable(exc):
                raise ShimError(
                    503, "engine_unavailable", f"Surya inference server: {type(exc).__name__}: {exc}"
                ) from exc
            raise
        return [
            reduce_page(prediction, number, image, with_image=options["include_page_images"])
            for number, (prediction, image) in enumerate(zip(predictions, images, strict=True), start=1)
        ]
