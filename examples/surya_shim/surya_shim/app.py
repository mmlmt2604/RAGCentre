"""A ``ragcentre.parser/v1`` service that parses documents with the Surya OCR 2.5 model.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route):

    include_page_images  return each rendered page as a base64 PNG (default false)
    ocr_figures          also read text inside pictures, figures and diagrams (default false)
    dpi                  render resolution for PDF pages, 72 to 400 (default 192)

Run it with::

    uvicorn surya_shim.app:app --port 8704

Surya runs its model in an inference server that it starts on first use (``llama-server`` from
llama.cpp on CPU and Apple Silicon, ``vllm`` in Docker on NVIDIA GPUs), or attaches to one named by
``SURYA_INFERENCE_BACKEND`` and ``SURYA_INFERENCE_URL``.

Environment: ``SURYA_SHIM_API_KEY`` requires ``Authorization: Bearer <key>`` (except on
``GET /info``).
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from shimkit import ShimError, boolean, file_extension, integer, page_range, parse_options
from shimkit import create_app as create_shim_app

from surya_shim.convert import to_ir
from surya_shim.engine import DEFAULT_DPI, SUPPORTED_EXTENSIONS, SuryaEngine

SHIM_VERSION = "0.2.0"
NAME = "surya-shim"
OPTIONS = [
    boolean("include_page_images", False, "Return each rendered page as a base64 PNG in pages[].image"),
    boolean("ocr_figures", False, "Also read text inside pictures, figures and diagrams"),
    integer("dpi", DEFAULT_DPI, minimum=72, maximum=400, description="Render resolution for PDF pages"),
    page_range(
        "page_range",
        "Only read these PDF pages: [first, last], 1-based and inclusive. Page numbers stay the original's",
    ),
]
MIME_TYPES = [
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/webp",
]

# (content, filename, options) -> reduced pages (see convert.py)
Engine = Callable[[bytes, str, dict[str, Any]], list[dict[str, Any]]]


def _surya_version() -> str:
    try:
        return version("surya-ocr")
    except PackageNotFoundError:
        return "unknown"


def _model_name() -> str | None:
    """The last path segment of ``SURYA_MODEL_CHECKPOINT`` (the model decides the output as much as
    the package does). ``surya.settings`` is light: the ``surya`` package imports lazily."""
    try:
        from surya.settings import settings

        return str(settings.SURYA_MODEL_CHECKPOINT).rstrip("/").rsplit("/", 1)[-1] or None
    except Exception:
        return None


def service_version() -> str:
    # RAGCentre pins this when the provider is created. A Surya upgrade or a different model changes
    # the output, so both are part of the version.
    model = _model_name()
    return f"{SHIM_VERSION}+surya.{_surya_version()}" + (f".{model}" if model else "")


def create_app(engine: Engine | None = None) -> FastAPI:
    run = engine or SuryaEngine()

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        opts = parse_options(OPTIONS, options)
        if file_extension(filename) not in SUPPORTED_EXTENSIONS:
            raise ShimError(415, "unsupported_media_type", f"{filename}: Surya reads PDF and image files only")
        if opts["page_range"] and file_extension(filename) != "pdf":
            raise ShimError(400, "invalid_options", "option 'page_range' only applies to PDF files")
        pages = run(content, filename, opts)
        return to_ir(pages, source_name=filename, include_page_images=opts["include_page_images"])

    return create_shim_app(
        name=NAME,
        version=service_version,
        mime_types=MIME_TYPES,
        capabilities={"page_images": True},
        options=OPTIONS,
        api_key_env="SURYA_SHIM_API_KEY",
        parse=parse,
        serialise=False,  # a client of the inference server, which does its own batching
        jobs=True,  # a scanned PDF can take longer than one request may last
        shutdown=getattr(run, "close", None),
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn surya_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import Surya.
    if name == "app":
        return create_app()
    raise AttributeError(name)
