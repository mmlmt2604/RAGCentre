"""Calls MinerU and reduces its result to a plain content list plus page sizes for ``convert``.

MinerU is imported on first use, so importing this module needs no engine. ``mineru.parse`` takes a
path, not bytes, so the upload is written to a temporary file.
"""

from __future__ import annotations

import os
import re
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


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and what it was raised from or while handling, nearest first (bounded, cycle-safe)."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending and len(seen) < 16:
        current = pending.pop(0)
        if current is None or any(current is s for s in seen):
            continue
        seen.append(current)
        pending += [current.__cause__, current.__context__]
    return seen


def _mro_has(exc: BaseException, name: str) -> bool:
    # Matched by name so that this module still imports without the engine (mineru_vl_utils).
    return any(cls.__name__ == name and cls.__module__.startswith("mineru_vl_utils") for cls in type(exc).__mro__)


# ``mineru_vl_utils.vlm_client.base_client.ServerError`` (a RuntimeError) is raised when the VLM server
# cannot be reached or answers with an error; ``HttpResponseError`` (``status_code``) and
# ``EngineUnavailableError`` derive from it. ``http_client.py`` raises a bare ``ServerError`` with the
# status only in the text ("Status code: 401, ..."), when it lists the server's models.
_STATUS = re.compile(r"[Ss]tatus code:? \[?(\d{3})")
# ``RequestError`` (a ValueError) is mostly about one request, but ``http_client.py`` also raises it
# for settings that can never work (http_client.py:239, 265, 281, 283, 289): the message says which.
_SETTING_ERRORS = (
    ("Invalid server URL", "MINERU_SHIM_VLM_SERVER_URL"),
    ("Model '", "MINERU_SHIM_VLM_MODEL"),
    ("No models found", "MINERU_SHIM_VLM_SERVER_URL"),
    ("Model name is empty", "MINERU_SHIM_VLM_MODEL"),
)


def translate_vlm_error(exc: Exception, vlm: VlmSettings) -> ShimError | None:
    """A failure of the vision-model backend as a ``503 engine_unavailable`` that names the setting to
    look at; ``None`` for anything else (a fault of the document, left to the service's ``422``).

    MinerU lets these escape as plain exceptions, not as ``MineruError``: ``ServerError`` when the server
    is down or refuses the request, ``RequestError`` for a setting the server rejects, and
    ``ImportError`` when the selected engine's package is not installed. All of them are properties of
    the service, so fixing its settings helps and re-sending the file does not."""
    for item in _chain(exc):
        if isinstance(item, ImportError):
            if vlm.server_url:
                hint = "install it in the service's environment"
            elif vlm.engine:
                hint = f"MINERU_SHIM_VLM_ENGINE is '{vlm.engine}': install it, change that setting, or set MINERU_SHIM_VLM_SERVER_URL"
            else:
                hint = (
                    "MinerU chose the engine itself: install it, or set MINERU_SHIM_VLM_ENGINE or "
                    "MINERU_SHIM_VLM_SERVER_URL"
                )
            return ShimError(
                503, "engine_unavailable", f"a package the VLM engine needs is not installed ({item}); {hint}"
            )
        if _mro_has(item, "RequestError"):
            for prefix, setting in _SETTING_ERRORS:
                if str(item).startswith(prefix):
                    return ShimError(
                        503, "engine_unavailable", f"the VLM server rejects {setting}; check its configuration"
                    )
        if _mro_has(item, "ServerError"):
            status = getattr(item, "status_code", None)
            if not isinstance(status, int) and (found := _STATUS.search(str(item))):
                status = int(found.group(1))
            if status in (401, 403):
                return ShimError(
                    503,
                    "engine_unavailable",
                    f"the VLM server refused the credentials (HTTP {status}): check MINERU_SHIM_VLM_API_KEY",
                )
            if (
                isinstance(getattr(item, "status_code", None), int)
                and status < 500
                and status not in (401, 403, 408, 429)
            ):
                continue  # an inference request was rejected; model-discovery failures remain 503
            setting = "MINERU_SHIM_VLM_SERVER_URL" if vlm.server_url else "the local VLM engine"
            detail = f" (HTTP {status})" if isinstance(status, int) else ""
            return ShimError(503, "engine_unavailable", f"the VLM server is not available{detail}; check {setting}")
    return None


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
                translated = translate_error(exc) or translate_vlm_error(exc, self.vlm)
                if translated is None:
                    raise
                raise translated from exc
            sizes = page_sizes(path, extension)
            if options.get("page_range") and extension == "pdf":
                first, last = (int(n) for n in options["page_range"].split("-"))
                sizes = {n: size for n, size in sizes.items() if first <= n <= last}  # only the pages parsed
            return content_list, sizes
