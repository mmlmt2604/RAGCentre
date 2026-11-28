"""Calls MinerU and reduces its result to a plain content list plus page sizes for ``convert``.

MinerU is imported on first use, so importing this module needs no engine. ``mineru.parse`` takes a
path, not bytes, so the upload is written to a temporary file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from shimkit import ShimError, file_extension, temp_upload

# mineru/filetypes.py: PDF and images support every tier; everything else is flash-only.
TIERED_EXTENSIONS = frozenset({"pdf", "png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "jp2"})
FLASH_ONLY_EXTENSIONS = frozenset(
    {
        "doc", "docx", "ppt", "pptx", "xls", "xlsx", "rtf", "odt", "ods", "odp",
        "html", "htm", "shtml", "mhtml", "mht", "csv", "tsv", "epub", "ofd",
    }
)  # fmt: skip
IMAGE_EXTENSIONS = TIERED_EXTENSIONS - {"pdf"}
SUPPORTED_EXTENSIONS = TIERED_EXTENSIONS | FLASH_ONLY_EXTENSIONS

# MinerU error types (mineru/errors.py) that mean "the engine is not ready or is busy": try later.
_RETRYABLE_TYPES = {"engine_error", "timeout_error", "rate_limit_error"}


def page_sizes(path: Path, extension: str) -> dict[int, tuple[float, float]]:
    """1-based page number -> (width, height): points for a PDF, pixels for an image, and ``{}`` for
    anything else (or when the file cannot be measured)."""
    try:
        if extension == "pdf":
            import pypdfium2 as pdfium

            document = pdfium.PdfDocument(str(path))
            try:
                return {number: tuple(page.get_size()) for number, page in enumerate(document, start=1)}
            finally:
                document.close()
        if extension in IMAGE_EXTENSIONS:
            from PIL import Image

            with Image.open(path) as image:
                return {1: (float(image.width), float(image.height))}
    except Exception:  # an unreadable file is MinerU's to report; boxes are simply left out
        return {}
    return {}


def translate_error(exc: Exception) -> ShimError | None:
    """A ``MineruError`` as a ``ShimError``; ``None`` for any other exception."""
    code = getattr(exc, "code", None)
    kind = getattr(exc, "type", None)
    if not isinstance(code, str) or not isinstance(kind, str):
        return None
    message = f"MinerU {code}: {exc}"
    if code == "file_type_unsupported":
        return ShimError(415, "unsupported_media_type", message)
    if kind in _RETRYABLE_TYPES:
        return ShimError(503, "engine_unavailable", message)
    return ShimError(422, "conversion_failed", message)


class MineruEngine:
    """``(content, filename, options) -> (content_list, page_sizes)``.

    In-process (the default) it is not thread-safe, and the app serialises calls. With ``api_url``
    it hands the file to a MinerU API server (``mineru-kit api-server``, or the hosted service) with
    ``mineru.MinerUApiParser``; the server runs the models, so ``image_analysis`` is the server's
    setting there."""

    def __init__(self, api_url: str | None = None, api_key: str | None = None) -> None:
        self.api_url = api_url
        self.api_key = api_key

    @classmethod
    def from_env(cls) -> MineruEngine:
        """Remote when ``MINERU_API_URL`` is set (``MINERU_API_KEY`` is its bearer token)."""
        return cls(os.environ.get("MINERU_API_URL") or None, os.environ.get("MINERU_API_KEY") or None)

    @property
    def remote(self) -> bool:
        return self.api_url is not None

    def __call__(
        self, content: bytes, filename: str, options: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[int, tuple[float, float]]]:
        try:
            import mineru
            from mineru.render.content_list import render_content_list
        except ImportError as exc:
            raise ShimError(503, "engine_unavailable", f"MinerU is not installed: {exc}") from exc
        extension = file_extension(filename)
        with temp_upload(content, filename) as path:
            try:
                if self.api_url is not None:
                    parser = mineru.MinerUApiParser(
                        api_url=self.api_url, api_key=self.api_key, tier=options["tier"], ocr_mode=options["ocr_mode"]
                    )
                    result = parser.parse(path, page_range=options.get("page_range", ""))
                else:
                    result = mineru.parse(
                        path,
                        tier=options["tier"],
                        ocr_mode=options["ocr_mode"],
                        image_analysis=options["image_analysis"],
                        page_range=options.get("page_range", ""),
                    )
                content_list = render_content_list(result.middle_json)
            except Exception as exc:
                translated = translate_error(exc)
                if translated is None:
                    raise
                raise translated from exc
            sizes = page_sizes(path, extension)
            if extension == "pdf" and options.get("page_range") and sizes:
                selected = {page.page_idx + 1 for page in result.middle_json.pages}
                sizes = {number: size for number, size in sizes.items() if number in selected}
            return content_list, sizes
