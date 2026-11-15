"""Calls MinerU and reduces its result to a plain content list plus page sizes for ``convert``.

MinerU is imported on first use, so importing this module needs no engine. ``mineru.parse`` takes a
path, not bytes, so the upload is written to a temporary file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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

# Every parameter of ``mineru.parse`` and field of ``VlmConfig`` is in one of these places (the live test
# ``test_engine_parameters_accounted_for`` compares them with the installed MinerU).
NAMED_PARAMETERS = {
    "parse": {
        "path": "the document", "tier": "option tier", "ocr_mode": "option ocr", "image_analysis": "option image_analysis",
        "page_range": "option page_range", "vlm_config": "the MINERU_SHIM_VLM_* settings",
    },
    "VlmConfig": {
        "engine": "MINERU_SHIM_VLM_ENGINE (in the version)", "model": "MINERU_SHIM_VLM_MODEL (in the version)",
        "server_url": "MINERU_SHIM_VLM_SERVER_URL", "api_key": "MINERU_SHIM_VLM_API_KEY",
    },
}  # fmt: skip
NOT_EXPOSED = {
    "parse": {"source_context": "only for HTML that came from a URL: the shim receives files"},
    "VlmConfig": {
        "http_timeout": "operational: MinerU's own configuration",
        "max_concurrency": "operational: MinerU's own configuration",
    },
}

# MinerU error types (mineru/errors.py) that mean "the engine is not ready or is busy": try later.
_RETRYABLE_TYPES = {"engine_error", "timeout_error", "rate_limit_error"}


@dataclass(frozen=True)
class VlmSettings:
    """Where MinerU's ``standard`` and ``advanced`` tiers find their vision-language model. Service
    settings, read from the environment at start-up (they are URLs and keys, not options):
    ``MINERU_SHIM_VLM_ENGINE`` (``auto``, ``llama-cpp``, ``vllm``, ``lmdeploy`` or ``mlx``),
    ``_MODEL``, ``_SERVER_URL`` and ``_API_KEY``. Only the engine and the model change the output, so
    only they are part of the reported version."""

    engine: str = ""
    model: str = ""
    server_url: str = ""
    api_key: str = ""

    @classmethod
    def from_env(cls) -> VlmSettings:
        def read(name: str) -> str:
            return os.environ.get(f"MINERU_SHIM_VLM_{name}", "").strip()

        return cls(read("ENGINE"), read("MODEL"), read("SERVER_URL"), read("API_KEY"))

    def configured(self) -> bool:
        return bool(self.engine or self.model or self.server_url or self.api_key)

    def vlm_config(self) -> Any:
        """MinerU's ``VlmConfig`` for these settings; ``None`` when none is set (MinerU then uses its
        own configuration)."""
        if not self.configured():
            return None
        from mineru.config import VlmConfig

        fields = {"engine": self.engine, "model": self.model, "server_url": self.server_url, "api_key": self.api_key}
        try:
            return VlmConfig(**{name: value for name, value in fields.items() if value})
        except Exception as exc:  # a bad engine name or URL in the environment
            raise ShimError(503, "engine_unavailable", f"invalid MINERU_SHIM_VLM_* settings: {exc}") from exc


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
    """``(content, filename, options) -> (content_list, page_sizes)``. Not thread-safe: the app
    serialises calls. ``options`` may carry ``page_range`` as ``"first-last"`` (1-based, inclusive)."""

    def __init__(self, vlm: VlmSettings | None = None) -> None:
        self.vlm = vlm if vlm is not None else VlmSettings.from_env()

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
            kwargs: dict[str, Any] = {
                "tier": options["tier"],
                "ocr_mode": options["ocr_mode"],
                "image_analysis": options["image_analysis"],
            }
            if options.get("page_range"):
                kwargs["page_range"] = options["page_range"]
            if (vlm_config := self.vlm.vlm_config()) is not None:
                kwargs["vlm_config"] = vlm_config
            try:
                result = mineru.parse(path, **kwargs)
                content_list = render_content_list(result.middle_json)
            except Exception as exc:
                translated = translate_error(exc)
                if translated is None:
                    raise
                raise translated from exc
            sizes = page_sizes(path, extension)
            if options.get("page_range") and extension == "pdf":
                first, last = (int(n) for n in options["page_range"].split("-"))
                sizes = {n: size for n, size in sizes.items() if first <= n <= last}  # only the pages parsed
            return content_list, sizes
