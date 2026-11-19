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
from shimkit import ShimError, boolean, choice, file_extension, integer, parse_options, string
from shimkit import create_app as create_shim_app

from paddleocr_shim.convert import to_ir
from paddleocr_shim.engine import SUPPORTED_EXTENSIONS, PaddleEngine, _vl_settings
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
    boolean("textline_orientation", None, "Correct upside-down or rotated text lines (the engine's default if unset)"),
    boolean("region_detection", None, "Detect layout regions before reading order (the engine's default if unset)"),
    boolean(
        "heading_levels",
        False,
        "Rank section titles with PaddleX's heading-level assignment (default: all level 2). "
        "PaddleOCR-VL always does this",
    ),
    choice(
        "pipeline",
        ["structure_v3", "vl"],
        "structure_v3",
        "structure_v3 is PP-StructureV3; vl is PaddleOCR-VL, a vision-language model that also reads tables, "
        "formulas and heading levels (set PADDLEOCR_VL_BACKEND and PADDLEOCR_VL_SERVER_URL to use a server you run)",
    ),
    string(
        "lang",
        None,
        "Language of the text models, e.g. en, ch, japan, korean (PP-StructureV3 only; default: PADDLEOCR_SHIM_LANG)",
    ),
    integer(
        "text_det_limit_side_len",
        None,
        minimum=32,
        maximum=8000,
        description="Longest-side limit, in pixels, for text detection input (PP-StructureV3 only): raise it for dense pages",
    ),
    boolean(
        "list_heuristic",
        False,
        "Treat paragraphs that start with a bullet or a number as list items (PaddleOCR has no list label)",
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
    vl = _vl_settings()  # the PaddleOCR-VL model and backend decide VL output
    suffix += "".join(f".vl-{vl[k]}" for k in ("pipeline_version", "vl_rec_backend") if k in vl)
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
        return to_ir(run(content, filename, opts), source_name=filename, list_heuristic=opts["list_heuristic"])

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
