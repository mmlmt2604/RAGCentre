"""A ``ragcentre.parser/v1`` service that parses documents with the Surya OCR 2.5 model.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route):

    include_page_images  return each rendered page as a base64 PNG (default false)
    ocr_figures          also read text inside pictures, figures and diagrams (default false)
    dpi                  render resolution for PDF pages, 72 to 400 (default 192)
    page_range           [first, last], 1-based and inclusive (default: the whole file)

There is no ``full_page`` option: the shim never gives Surya layout results, so Surya always reads
whole pages (``full_page=False`` would raise).

Run it with::

    uvicorn surya_shim.app:app --port 8704

Surya runs its model in an inference server that it starts on first use (``llama-server`` from
llama.cpp on CPU and Apple Silicon, ``vllm`` in Docker on NVIDIA GPUs), or attaches to one named by
``SURYA_INFERENCE_BACKEND`` and ``SURYA_INFERENCE_URL``.

Environment: ``SURYA_SHIM_API_KEY`` requires ``Authorization: Bearer <key>`` (except on
``GET /info``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from pydantic import Field
from shimkit import PAGE_RANGE_DESCRIPTION, PageRange, ShimError, ShimOptions, file_extension
from shimkit import create_app as create_shim_app

from surya_shim.convert import to_ir
from surya_shim.engine import DEFAULT_DPI, SUPPORTED_EXTENSIONS, SuryaEngine

SHIM_VERSION = "0.3.0"
DEFAULT_GGUF_FILE = "surya-2.5.gguf"  # surya.settings.SURYA_GGUF_MODEL_FILE
NAME = "surya-shim"
MIME_TYPES = [
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/webp",
]


class Options(ShimOptions):
    include_page_images: bool = Field(False, description="Return each rendered page as a base64 PNG.")
    ocr_figures: bool = Field(False, description="Also read text inside pictures, figures and diagrams.")
    dpi: int = Field(DEFAULT_DPI, ge=72, le=400, description="Render resolution for PDF pages.")
    page_range: PageRange | None = Field(
        None,
        description=PAGE_RANGE_DESCRIPTION + " Pages are numbered as in the file. Default: the whole file.",
    )


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


def _suffix(value: str) -> str:
    """``value`` with every character outside ``[A-Za-z0-9_-]`` replaced by ``-``."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)


def _backend() -> str | None:
    """The inference backend Surya will use: ``SURYA_INFERENCE_BACKEND`` if set, else what Surya's
    own auto-detection picks (vLLM on an NVIDIA GPU, otherwise llama.cpp). ``None`` when Surya is not
    importable. ``surya.inference._autodetect_backend`` is private; if it goes missing the backend
    is reported as ``auto``."""
    try:
        from surya.settings import settings

        if settings.SURYA_INFERENCE_BACKEND:
            return str(settings.SURYA_INFERENCE_BACKEND).lower()
    except Exception:
        return None
    try:
        from surya.inference import _autodetect_backend

        return str(_autodetect_backend()).lower()
    except Exception:
        return "auto"


def _gguf_name() -> str | None:
    """The llama.cpp model file when it is not the default one. Surya uses
    ``SURYA_GGUF_LOCAL_MODEL_PATH`` only together with ``SURYA_GGUF_LOCAL_MMPROJ_PATH``
    (``surya/inference/backends/llamacpp.py``), so the path counts only when both are set."""
    try:
        from surya.settings import settings

        if settings.SURYA_GGUF_LOCAL_MODEL_PATH and settings.SURYA_GGUF_LOCAL_MMPROJ_PATH:
            name = str(settings.SURYA_GGUF_LOCAL_MODEL_PATH).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        else:
            name = str(settings.SURYA_GGUF_MODEL_FILE)
    except Exception:
        return None
    return name if name and name != DEFAULT_GGUF_FILE else None


def service_version() -> str:
    # RAGCentre pins this when the provider is created. A Surya upgrade, a different model, the
    # inference backend or the llama.cpp model file changes the output, so all are part of the version.
    version_ = f"{SHIM_VERSION}+surya.{_surya_version()}"
    if model := _model_name():
        version_ += f".{model}"
    if backend := _backend():
        version_ += f".{_suffix(backend)}"
        if backend == "llamacpp" and (gguf := _gguf_name()):
            version_ += f".gguf-{_suffix(gguf)}"
    return version_


def create_app(engine: Engine | None = None) -> FastAPI:
    run = engine or SuryaEngine()

    def parse(content: bytes, filename: str, options: Options) -> dict[str, Any]:
        opts = options.model_dump()
        if file_extension(filename) not in SUPPORTED_EXTENSIONS:
            raise ShimError(415, "unsupported_media_type", f"{filename}: Surya reads PDF and image files only")
        pages = run(content, filename, opts)
        return to_ir(pages, source_name=filename, include_page_images=opts["include_page_images"])

    return create_shim_app(
        name=NAME,
        version=service_version,
        mime_types=MIME_TYPES,
        capabilities={"page_images": True},
        api_key_env="SURYA_SHIM_API_KEY",
        parse=parse,
        options_model=Options,
        serialise=False,  # a client of the inference server, which does its own batching
        shutdown=getattr(run, "close", None),
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn surya_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import Surya.
    if name == "app":
        return create_app()
    raise AttributeError(name)
