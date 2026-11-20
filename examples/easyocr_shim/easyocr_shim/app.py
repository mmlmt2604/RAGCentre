"""A ``ragcentre.parser/v1`` service that reads PDFs and images with EasyOCR.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

EasyOCR finds and reads lines of text; it has no layout analysis. The shim groups the lines into
paragraphs by position and, with the ``headings`` option, guesses headings from the text height
(``shimkit.layout``). Tables, figures and lists come out as paragraphs.

Run it with::

    uvicorn easyocr_shim.app:app --port 8705

Environment: ``EASYOCR_SHIM_API_KEY`` requires ``Authorization: Bearer <key>`` (except on
``GET /info``); ``EASYOCR_SHIM_LANGS`` sets the default languages (comma-separated, default ``en``);
``EASYOCR_SHIM_GPU=0`` forces the CPU.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from shimkit import (
    ShimError,
    boolean,
    choice,
    file_extension,
    integer,
    lines_to_ir,
    page_range,
    parse_options,
    strings,
)
from shimkit import create_app as create_shim_app

from easyocr_shim.engine import DEFAULT_CANVAS_SIZE, DEFAULT_DPI, SUPPORTED_EXTENSIONS, EasyOcrEngine

SHIM_VERSION = "0.1.0"
NAME = "easyocr-shim"
MIME_TYPES = [
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/bmp",
    "image/tiff",
    "image/webp",
]
OPTIONS = [
    strings(
        "languages",
        'EasyOCR language codes, e.g. ["en", "de"] (default: EASYOCR_SHIM_LANGS, else English). '
        "Some scripts can only be combined with English",
    ),
    integer("dpi", DEFAULT_DPI, minimum=72, maximum=400, description="Render resolution for PDF pages"),
    page_range(
        "page_range", "Only read these pages: [first, last], 1-based and inclusive. Page numbers stay the original's"
    ),
    boolean("paragraphs", True, "Group text lines into paragraphs by their position (off: one block per line)"),
    boolean("headings", False, "Guess headings: short lines whose text is clearly taller than the body text"),
    integer(
        "min_confidence",
        0,
        minimum=0,
        maximum=100,
        description="Drop text read with a confidence below this, in percent",
    ),
    choice("decoder", ["greedy", "beamsearch", "wordbeamsearch"], "greedy", "Text decoder (beam search is slower)"),
    boolean("rotated_text", False, "Also try each text box turned by 90, 180 and 270 degrees (slower)"),
    integer(
        "canvas_size",
        DEFAULT_CANVAS_SIZE,
        minimum=256,
        maximum=8192,
        description="Longest side, in pixels, that text detection works at: larger pages are scaled down",
    ),
    boolean("include_page_images", False, "Return each page image as a base64 PNG in pages[].image"),
]

# (content, filename, options) -> line pages (see engine.py)
Engine = Callable[[bytes, str, dict[str, Any]], list[dict[str, Any]]]


def _easyocr_version() -> str:
    try:
        return version("easyocr")
    except PackageNotFoundError:
        return "unknown"


def service_version() -> str:
    # RAGCentre pins this when the provider is created. An EasyOCR upgrade changes the output, and
    # so do the default languages, so both are part of the version. (Per-route languages are in the
    # route's options.)
    langs = os.environ.get("EASYOCR_SHIM_LANGS", "").strip()
    suffix = ".lang-" + "+".join(c.strip() for c in langs.split(",") if c.strip()) if langs else ""
    return f"{SHIM_VERSION}+easyocr.{_easyocr_version()}{suffix}"


def create_app(engine: Engine | None = None) -> FastAPI:
    run = engine or EasyOcrEngine()

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        opts = parse_options(OPTIONS, options)
        if file_extension(filename) not in SUPPORTED_EXTENSIONS:
            raise ShimError(415, "unsupported_media_type", f"{filename}: EasyOCR reads PDF and image files only")
        return lines_to_ir(
            run(content, filename, opts),
            source_name=filename,
            paragraphs=opts["paragraphs"],
            headings=opts["headings"],
            min_confidence=opts["min_confidence"] / 100,
            include_page_images=opts["include_page_images"],
        )

    return create_shim_app(
        name=NAME,
        version=service_version,
        mime_types=MIME_TYPES,
        capabilities={"page_images": True},
        options=OPTIONS,
        api_key_env="EASYOCR_SHIM_API_KEY",
        parse=parse,
        serialise=True,  # one reader, one document at a time
        jobs=True,  # a scanned PDF can take longer than one request may last
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn easyocr_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import EasyOCR.
    if name == "app":
        return create_app()
    raise AttributeError(name)
