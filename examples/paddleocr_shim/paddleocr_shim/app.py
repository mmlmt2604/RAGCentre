"""A ``ragcentre.parser/v1`` service that parses documents with PaddleOCR (PP-StructureV3).

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route), all booleans:

    tables       table recognition (default true)
    formulas     formula recognition (default true)
    charts       chart recognition (default false)
    seals        seal recognition (default false)
    orientation  document orientation classification (default false)
    unwarp       document unwarping (default false)
    heading_levels  rank section titles with PaddleX's heading-level assignment (default false:
                 every section title is level 2)

Run it with::

    uvicorn paddleocr_shim.app:app --port 8703

Set ``PADDLEX_SERVING_URL`` to use a running PaddleX serving instance instead of loading PaddleOCR
here (see ``remote.py``).

Environment: ``PADDLEOCR_SHIM_API_KEY`` requires ``Authorization: Bearer <key>`` (except on
``GET /info``); ``PADDLEOCR_SHIM_LANG`` sets PaddleOCR's ``lang`` for the text models at start-up.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from shimkit import ShimError, boolean, file_extension, parse_options
from shimkit import create_app as create_shim_app

from paddleocr_shim.convert import to_ir
from paddleocr_shim.engine import SUPPORTED_EXTENSIONS, PaddleEngine
from paddleocr_shim.remote import PaddleServingEngine

SHIM_VERSION = "0.1.0"
NAME = "paddleocr-shim"
MIME_TYPES = [
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/bmp",
    "image/tiff",
    "image/webp",
]
OPTIONS = [
    boolean("tables", True, "Table recognition"),
    boolean("formulas", True, "Formula recognition"),
    boolean("charts", False, "Chart recognition"),
    boolean("seals", False, "Seal recognition"),
    boolean("orientation", False, "Document orientation classification"),
    boolean("unwarp", False, "Document unwarping"),
    boolean(
        "heading_levels", False, "Rank section titles with PaddleX's heading-level assignment (default: all level 2)"
    ),
]

# (content, filename, options) -> reduced pages (see convert.py)
Engine = Callable[[bytes, str, dict[str, bool]], list[dict[str, Any]]]


def _paddleocr_version() -> str:
    try:
        return version("paddleocr")
    except PackageNotFoundError:
        return "unknown"


def service_version(lang: str | None = None, remote: bool = False) -> str:
    # RAGCentre pins this when the provider is created. A PaddleOCR upgrade or a different ``lang``
    # changes the output, so both are part of the version. PaddleX serving reports no version, so a
    # remote server is just ``remote``: set the provider's ``revision`` when you change its models.
    if remote:
        return f"{SHIM_VERSION}+paddlex-serving.remote"
    suffix = f".lang-{lang}" if lang else ""
    return f"{SHIM_VERSION}+paddleocr.{_paddleocr_version()}{suffix}"


def create_app(engine: Engine | None = None) -> FastAPI:
    lang = os.environ.get("PADDLEOCR_SHIM_LANG") or None
    remote = engine if isinstance(engine, PaddleServingEngine) else None
    if engine is None:
        remote = PaddleServingEngine.from_env()
    run = engine or remote or PaddleEngine(lang=lang)

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        opts = parse_options(OPTIONS, options)
        if file_extension(filename) not in SUPPORTED_EXTENSIONS:
            raise ShimError(415, "unsupported_media_type", f"{filename}: PaddleOCR reads PDF and image files only")
        return to_ir(run(content, filename, opts), source_name=filename)

    return create_shim_app(
        name=NAME,
        version=lambda: service_version(lang, remote is not None),
        mime_types=MIME_TYPES,
        capabilities={"page_images": False},
        options=OPTIONS,
        api_key_env="PADDLEOCR_SHIM_API_KEY",
        parse=parse,
        serialise=remote is None,  # PaddlePaddle pipelines are not safe to call concurrently
        jobs=True,  # a scanned PDF can take longer than one request may last
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn paddleocr_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import PaddleOCR.
    if name == "app":
        return create_app()
    raise AttributeError(name)
