"""A ``ragcentre.parser/v1`` service that parses documents with MinerU.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route):

    tier            ``flash``, ``basic``, ``standard`` or ``advanced`` (default ``standard``). Only PDFs
                    and images support every tier; other formats are always parsed with ``flash``.
    ocr             ``true`` forces OCR, ``false`` reads the text layer, ``"auto"`` decides
                    (default ``"auto"``)
    image_analysis  describe pictures with a vision model (default false; MinerU's own default is
                    true, which needs a VLM for every picture)

Run it with::

    uvicorn mineru_shim.app:app --port 8702

Environment: ``MINERU_SHIM_API_KEY`` requires ``Authorization: Bearer <key>`` (except on
``GET /info``).
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from shimkit import ShimError, bool_option, choice_option, file_extension
from shimkit import create_app as create_shim_app

from mineru_shim.convert import PageSizes, to_ir
from mineru_shim.engine import SUPPORTED_EXTENSIONS, TIERED_EXTENSIONS, MineruEngine

SHIM_VERSION = "0.1.0"
NAME = "mineru-shim"
TIERS = ["flash", "basic", "standard", "advanced"]
# mineru/filetypes.py (MIME_TYPE_BY_EXTENSION), for the extensions MinerU parses.
MIME_TYPES = [
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/tiff",
    "image/jp2",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/msword",
    "application/vnd.ms-powerpoint",
    "application/vnd.ms-excel",
    "application/rtf",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "text/html",
    "multipart/related",
    "text/csv",
    "text/tab-separated-values",
    "application/epub+zip",
    "application/ofd",
]

# (content, filename, options) -> (content list, page sizes) (see convert.py)
Engine = Callable[[bytes, str, dict[str, Any]], tuple[list[dict[str, Any]], PageSizes]]


def _mineru_version() -> str:
    try:
        return version("mineru")
    except PackageNotFoundError:
        return "unknown"


def service_version() -> str:
    # RAGCentre pins this when the provider is created; a MinerU upgrade changes parse output.
    return f"{SHIM_VERSION}+mineru.{_mineru_version()}"


def _ocr_mode(options: dict[str, Any]) -> str:
    value = options.get("ocr", "auto")
    if value is True:
        return "ocr"
    if value is False:
        return "txt"
    if value == "auto":
        return "auto"
    raise ShimError(400, "invalid_options", "option 'ocr' must be true, false or \"auto\"")


def create_app(engine: Engine | None = None) -> FastAPI:
    run = engine or MineruEngine()

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        extension = file_extension(filename)
        tier = choice_option(options, "tier", TIERS, "standard")
        opts = {
            "tier": tier if extension in TIERED_EXTENSIONS else "flash",
            "ocr_mode": _ocr_mode(options),
            "image_analysis": bool_option(options, "image_analysis", False),
        }
        if extension not in SUPPORTED_EXTENSIONS:
            raise ShimError(
                415, "unsupported_media_type", f"{filename}: MinerU does not read .{extension or '?'} files"
            )
        content_list, sizes = run(content, filename, opts)
        return to_ir(content_list, sizes, source_name=filename)

    return create_shim_app(
        name=NAME,
        version=service_version,
        mime_types=MIME_TYPES,
        capabilities={"page_images": False},
        api_key_env="MINERU_SHIM_API_KEY",
        parse=parse,
        serialise=True,  # MinerU runs local models and may start an inference server
        jobs=True,  # a scanned PDF can take longer than one request may last
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn mineru_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import MinerU.
    if name == "app":
        return create_app()
    raise AttributeError(name)
