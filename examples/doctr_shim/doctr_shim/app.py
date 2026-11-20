"""A ``ragcentre.parser/v1`` service that reads PDFs and images with Mindee's docTR.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

docTR detects and reads words and lines; it has no layout analysis of its own here. The shim groups
the lines into paragraphs by position and, with the ``headings`` option, guesses headings from the
text height (``shimkit.layout``). Tables, figures and lists come out as paragraphs.

Run it with::

    uvicorn doctr_shim.app:app --port 8706

Environment: ``DOCTR_SHIM_API_KEY`` requires ``Authorization: Bearer <key>`` (except on
``GET /info``); ``DOCTR_SHIM_DEVICE`` is ``auto`` (default: CUDA if available, else CPU), ``cpu``,
``cuda`` or ``mps``.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from shimkit import ShimError, boolean, choice, file_extension, integer, lines_to_ir, page_range, parse_options
from shimkit import create_app as create_shim_app

from doctr_shim.engine import DEFAULT_DPI, DETECTION, RECOGNITION, SUPPORTED_EXTENSIONS, DoctrEngine

SHIM_VERSION = "0.1.0"
NAME = "doctr-shim"
MIME_TYPES = [
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/bmp",
    "image/tiff",
    "image/webp",
]
OPTIONS = [
    choice("detection", DETECTION, DETECTION[0], "Text-detection model (fast_* are quicker, db_resnet50 is sturdier)"),
    choice(
        "recognition",
        RECOGNITION,
        RECOGNITION[0],
        "Text-recognition model (crnn_mobilenet_* are lighter; parseq and master are slower)",
    ),
    boolean(
        "straight_pages",
        True,
        "Assume text lines are horizontal (faster). Turn off for rotated, skewed or photographed pages",
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
    boolean("include_page_images", False, "Return each page image as a base64 PNG in pages[].image"),
]

# (content, filename, options) -> line pages (see engine.py)
Engine = Callable[[bytes, str, dict[str, Any]], list[dict[str, Any]]]


def _doctr_version() -> str:
    try:
        return version("python-doctr")
    except PackageNotFoundError:
        return "unknown"


def service_version() -> str:
    # RAGCentre pins this when the provider is created. A docTR upgrade can change the pretrained
    # weights and so the output. The model choice is a route option, so it is not part of it.
    return f"{SHIM_VERSION}+doctr.{_doctr_version()}"


def create_app(engine: Engine | None = None) -> FastAPI:
    run = engine or DoctrEngine()

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        opts = parse_options(OPTIONS, options)
        if file_extension(filename) not in SUPPORTED_EXTENSIONS:
            raise ShimError(415, "unsupported_media_type", f"{filename}: docTR reads PDF and image files only")
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
        api_key_env="DOCTR_SHIM_API_KEY",
        parse=parse,
        serialise=True,  # one model in memory, one document at a time
        jobs=True,  # a scanned PDF can take longer than one request may last
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn doctr_shim.app:app`` builds the real app lazily, so importing this module (in tests)
    # does not import docTR or PyTorch.
    if name == "app":
        return create_app()
    raise AttributeError(name)
