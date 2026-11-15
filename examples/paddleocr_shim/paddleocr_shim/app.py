"""A ``ragcentre.parser/v1`` service that parses documents with PaddleOCR (PP-StructureV3).

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route; ``/info`` reports them as ``options_schema``, and an unknown
or mistyped one is ``400 invalid_options``):

    tables, formulas, charts, seals, orientation, unwarp, heading_levels   on/off features
    languages             one PaddleOCR language code (replaces PADDLEOCR_SHIM_LANG for the route)
    ocr_version           PP-OCRv3, PP-OCRv4 or PP-OCRv5
    textline_orientation, region_detection   on/off, default PP-StructureV3's own
    engine                other PPStructureV3 arguments, after a check (see ``engine.py``)

PP-StructureV3 has no page selection, so there is no ``page_range``.

Run it with::

    uvicorn paddleocr_shim.app:app --port 8703

Environment: ``PADDLEOCR_SHIM_API_KEY`` requires ``Authorization: Bearer <key>`` (except on
``GET /info``); ``PADDLEOCR_SHIM_LANG`` sets PaddleOCR's ``lang`` for the text models at start-up.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import Field
from shimkit import ShimError, ShimOptions, file_extension
from shimkit import create_app as create_shim_app

from paddleocr_shim.convert import to_ir
from paddleocr_shim.engine import SUPPORTED_EXTENSIONS, PaddleEngine, split_engine

SHIM_VERSION = "0.3.0"
NAME = "paddleocr-shim"
MIME_TYPES = [
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/bmp",
    "image/tiff",
    "image/webp",
]


class Options(ShimOptions):
    tables: bool = Field(True, description="Table recognition.")
    formulas: bool = Field(True, description="Formula recognition.")
    charts: bool = Field(False, description="Chart recognition.")
    seals: bool = Field(False, description="Seal recognition.")
    orientation: bool = Field(False, description="Document orientation classification.")
    unwarp: bool = Field(False, description="Document unwarping.")
    heading_levels: bool = Field(
        False, description="Rank section titles with PaddleX's heading-level assignment (otherwise all are level 2)."
    )
    languages: list[str] | None = Field(
        None,
        min_length=1,
        max_length=1,
        description="OCR language, one PaddleOCR code (for example en, ch, japan, fr). Replaces "
        "PADDLEOCR_SHIM_LANG for this route; default: the service's setting.",
    )
    ocr_version: Literal["PP-OCRv3", "PP-OCRv4", "PP-OCRv5"] | None = Field(
        None, description="Text detection and recognition model generation. Default: PaddleOCR's own."
    )
    textline_orientation: bool | None = Field(
        None, description="Classify the orientation of each text line. Default: PP-StructureV3's own."
    )
    region_detection: bool | None = Field(
        None, description="Detect document regions before layout analysis. Default: PP-StructureV3's own."
    )
    engine: dict[str, Any] = Field(
        default_factory=dict,
        description="Other PPStructureV3 arguments (thresholds, model names, table switches), passed to its "
        "constructor and predict(). Model directories and batch sizes are refused. See "
        "https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PP-StructureV3.html",
    )


# (content, filename, options) -> reduced pages (see convert.py)
Engine = Callable[[bytes, str, dict[str, Any]], list[dict[str, Any]]]


def _paddleocr_version() -> str:
    try:
        return version("paddleocr")
    except PackageNotFoundError:
        return "unknown"


def service_version(lang: str | None = None) -> str:
    # RAGCentre pins this when the provider is created. A PaddleOCR upgrade or a different ``lang``
    # changes the output, so both are part of the version.
    suffix = f".lang-{lang}" if lang else ""
    return f"{SHIM_VERSION}+paddleocr.{_paddleocr_version()}{suffix}"


def create_app(engine: Engine | None = None) -> FastAPI:
    lang = os.environ.get("PADDLEOCR_SHIM_LANG") or None
    run = engine or PaddleEngine(lang=lang)

    def parse(content: bytes, filename: str, options: Options) -> dict[str, Any]:
        opts = options.model_dump()
        split_engine(options.engine)  # a denied or unknown key is a 400 whatever the engine
        if file_extension(filename) not in SUPPORTED_EXTENSIONS:
            raise ShimError(415, "unsupported_media_type", f"{filename}: PaddleOCR reads PDF and image files only")
        return to_ir(run(content, filename, opts), source_name=filename)

    return create_shim_app(
        name=NAME,
        version=lambda: service_version(lang),
        mime_types=MIME_TYPES,
        capabilities={"page_images": False},
        api_key_env="PADDLEOCR_SHIM_API_KEY",
        parse=parse,
        options_model=Options,
        serialise=True,  # PaddlePaddle pipelines are not safe to call concurrently
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn paddleocr_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import PaddleOCR.
    if name == "app":
        return create_app()
    raise AttributeError(name)
