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

# Every parameter of the Surya calls the shim makes is in one of these places (the live test
# ``test_engine_parameters_accounted_for`` compares them with the installed Surya).
NAMED_PARAMETERS = {
    "RecognitionPredictor.__call__": {"images": "the pages", "skip_labels": "option ocr_figures"},
    "load_pdf": {"pdf_path": "the document", "page_range": "option page_range", "dpi": "option dpi"},
}
NOT_EXPOSED = {
    "RecognitionPredictor.__call__": {
        "layout_results": "never given: Surya then reads whole pages, which is its more accurate mode",
        "full_page": "True is what Surya picks without layout results; False raises without them",
    },
    "load_pdf": {},
}

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
                "confidence": block.confidence,
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

        # Page numbers in the IR are the pages' numbers in the file, whatever range is parsed.
        first, last = options.get("page_range") or (None, None)
        numbers: list[int] | None = None
        with temp_upload(content, filename) as path:
            if file_extension(filename) == "pdf":
                if first is None:
                    images, _ = load_pdf(path, dpi=options["dpi"])
                else:
                    # load_pdf takes 0-based page indices and rejects any outside the file.
                    try:
                        images, _ = load_pdf(path, page_range=list(range(first - 1, last)), dpi=options["dpi"])
                    except ValueError as exc:
                        raise ShimError(400, "invalid_options", f"option 'page_range' {[first, last]}: {exc}") from exc
                    numbers = list(range(first, last + 1))
            else:
                if first is not None and first > 1:
                    raise ShimError(
                        400, "invalid_options", f"option 'page_range' {[first, last]} is outside this 1-page image"
                    )
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
            for number, (prediction, image) in zip(
                numbers or range(1, len(images) + 1), zip(predictions, images, strict=True), strict=True
            )
        ]
