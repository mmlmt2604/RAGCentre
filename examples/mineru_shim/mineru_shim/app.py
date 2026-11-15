"""A ``ragcentre.parser/v1`` service that parses documents with MinerU.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route):

    tier            ``flash``, ``basic``, ``standard`` or ``advanced`` (default ``standard``). Only PDFs
                    and images support every tier; other formats are always parsed with ``flash``.
    ocr             ``true`` forces OCR, ``false`` prefers the text layer (above ``flash`` MinerU still
                    OCRs regions that have no usable text), ``"auto"`` decides (default ``"auto"``)
    image_analysis  describe pictures with a vision model. Only the ``advanced`` tier does this: MinerU
                    ignores it at every other tier (default false; MinerU's own default is true,
                    which needs a VLM for every picture)
    page_range      ``[first, last]``, 1-based and inclusive; PDFs only (default: the whole file)

Service settings (environment, none of them an option): ``MINERU_SHIM_VLM_ENGINE`` and
``MINERU_SHIM_VLM_MODEL`` (part of the reported version), ``MINERU_SHIM_VLM_SERVER_URL`` and
``MINERU_SHIM_VLM_API_KEY`` point the ``standard`` and ``advanced`` tiers at a VLM server. A failing
VLM (server down, key refused, engine package missing) is ``503 engine_unavailable`` naming the setting.

Run it with::

    uvicorn mineru_shim.app:app --port 8702

Environment: ``MINERU_SHIM_API_KEY`` requires ``Authorization: Bearer <key>`` (except on
``GET /info``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import Field
from shimkit import PAGE_RANGE_DESCRIPTION, PageRange, ShimError, ShimOptions, file_extension
from shimkit import create_app as create_shim_app

from mineru_shim.convert import PageSizes, to_ir
from mineru_shim.engine import SUPPORTED_EXTENSIONS, TIERED_EXTENSIONS, MineruEngine, VlmSettings

SHIM_VERSION = "0.4.0"
NAME = "mineru-shim"
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


class Options(ShimOptions):
    tier: Literal["flash", "basic", "standard", "advanced"] = Field(
        "standard",
        description="MinerU tier. Only PDFs and images support every tier; other formats are always parsed with flash.",
    )
    ocr: bool | Literal["auto"] = Field(
        "auto",
        description='true forces OCR, false prefers the text layer (above flash, MinerU still OCRs regions without text), "auto" decides.',
    )
    image_analysis: bool = Field(
        False,
        description=(
            "Describe pictures with a vision model. Only the advanced tier does this; MinerU ignores it at "
            "every other tier. MinerU's own default is true, which needs a VLM."
        ),
    )
    page_range: PageRange | None = Field(
        None,
        description=PAGE_RANGE_DESCRIPTION + " PDF only; pages are numbered as in the file. Default: the whole file.",
    )


# (content, filename, options) -> (content list, page sizes) (see convert.py)
Engine = Callable[[bytes, str, dict[str, Any]], tuple[list[dict[str, Any]], PageSizes]]


def _mineru_version() -> str:
    try:
        return version("mineru")
    except PackageNotFoundError:
        return "unknown"


def service_version() -> str:
    # RAGCentre pins this when the provider is created. A MinerU upgrade, or a different VLM engine or
    # model for the standard and advanced tiers, changes parse output. The VLM server's URL and key do not.
    version_ = f"{SHIM_VERSION}+mineru.{_mineru_version()}"
    vlm = VlmSettings.from_env()
    if vlm.engine or vlm.model:
        version_ += ".vlm-" + "-".join(
            re.sub(r"[^A-Za-z0-9_-]", "-", part) for part in (vlm.engine or "auto", vlm.model) if part
        )
    return version_


def _ocr_mode(value: bool | str) -> str:
    if value is True:
        return "ocr"
    if value is False:
        return "txt"
    return "auto"


def create_app(engine: Engine | None = None) -> FastAPI:
    run = engine or MineruEngine()

    def parse(content: bytes, filename: str, options: Options) -> dict[str, Any]:
        extension = file_extension(filename)
        opts = {
            "tier": options.tier if extension in TIERED_EXTENSIONS else "flash",
            "ocr_mode": _ocr_mode(options.ocr),
            "image_analysis": options.image_analysis,
        }
        if options.page_range is not None and extension == "pdf":
            opts["page_range"] = f"{options.page_range[0]}-{options.page_range[1]}"
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
        options_model=Options,
        serialise=True,  # MinerU runs local models and may start an inference server
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn mineru_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import MinerU.
    if name == "app":
        return create_app()
    raise AttributeError(name)
