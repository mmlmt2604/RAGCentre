"""A ``ragcentre.parser/v1`` service that parses documents with Docling.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route) are described by ``options.py`` and reported by ``/info``
as ``options_schema``. Any other key, or a value of the wrong type, is ``400 invalid_options``.
They apply to PDFs and to image files. ``engine`` hands Docling's own ``PdfPipelineOptions`` fields
through, after a check that refuses operational ones (paths, URLs, devices, batch sizes).

Run it with::

    uvicorn docling_shim.app:app --port 8701

Set ``DOCLING_SHIM_API_KEY`` to require ``Authorization: Bearer <key>`` (give RAGCentre the same
value as the provider's ``api_key`` secret). ``GET /info`` stays open: RAGCentre reads it without
credentials when it creates the provider.

Service settings (environment, read at start-up; none of them is an option): ``DOCLING_SHIM_DEVICE``
and ``DOCLING_SHIM_NUM_THREADS`` (accelerator), ``DOCLING_SHIM_PICTURE_DESCRIPTION_URL``,
``_MODEL`` and ``_API_KEY`` (describe pictures with an OpenAI-compatible server instead of
Docling's local model). The model is part of the reported version; the URL and key are not.
"""

from __future__ import annotations

import hmac
import importlib.util
import json
import os
import platform
import re
import sys
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from docling_shim.convert import to_ir
from docling_shim.engine import MAX_CONVERTERS, DoclingConverter, Settings, ShimError
from docling_shim.options import Options, OptionsError, parse_options

__all__ = ["MAX_CONVERTERS", "DoclingConverter", "Options", "create_app", "service_version"]

SHIM_VERSION = "0.3.0"
PROTOCOL = "ragcentre.parser/v1"
MIME_TYPES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/html",
    "text/markdown",
    "text/csv",
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/webp",
]


# (content, filename, options) -> DoclingDocument
Converter = Callable[[bytes, str, Options], Any]


def _docling_version() -> str:
    try:
        return version("docling")
    except PackageNotFoundError:
        return "unknown"


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def resolve_ocr_engine() -> str:
    """The OCR engine Docling's automatic default (``OcrAutoOptions``) picks, worked out the way
    ``docling/models/stages/ocr/auto_ocr_model.py`` does but without loading any model: the first
    that is installed of ocrmac (macOS), Nemotron (Linux x86-64, Python 3.12, CUDA), RapidOCR with
    onnxruntime, EasyOCR, RapidOCR with torch. ``none`` when there is none."""
    if sys.platform == "darwin" and _has("ocrmac"):
        return "ocrmac"
    if (
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and sys.version_info[:2] == (3, 12)
        and _has("nemotron_ocr")
    ):
        try:
            import torch

            if torch.cuda.is_available():
                return "nemotron"
        except Exception:  # not usable: Docling falls through to the next engine
            pass
    if _has("onnxruntime") and _has("rapidocr"):
        return "rapidocr-onnxruntime"
    if _has("easyocr"):
        return "easyocr"
    if _has("torch") and _has("rapidocr"):
        return "rapidocr-torch"
    return "none"


def _suffix(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)


def service_version() -> str:
    # RAGCentre pins this when the provider is created. A Docling upgrade, a different automatic OCR
    # engine or a different picture-description model changes parse output, so all are part of the
    # version. Hardware settings, the description URL and its key are not.
    version_ = f"{SHIM_VERSION}+docling.{_docling_version()}.ocr-{_suffix(resolve_ocr_engine())}"
    if model := Settings.from_env().picture_description_model:
        version_ += f".desc-{_suffix(model)}"
    return version_


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(converter: Converter | None = None) -> FastAPI:
    convert = converter or DoclingConverter()
    api_key = os.environ.get("DOCLING_SHIM_API_KEY") or None
    options_schema = Options.model_json_schema()
    app = FastAPI(title="Docling parser shim for RAGCentre", version=SHIM_VERSION)

    @app.middleware("http")
    async def check_key(request: Request, call_next: Any) -> Any:
        # /info is open: RAGCentre reads it without credentials when it creates the provider.
        if api_key is not None and not (request.method == "GET" and request.url.path == "/info"):
            given = request.headers.get("authorization", "")
            if not hmac.compare_digest(given.encode(), f"Bearer {api_key}".encode()):
                return error(401, "unauthorized", "missing or wrong bearer token")
        return await call_next(request)

    @app.get("/info")
    def info() -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "name": "docling-shim",
            "version": service_version(),
            "mime_types": MIME_TYPES,
            "capabilities": {"page_images": True},
            "options_schema": options_schema,
        }

    @app.post("/parse", response_model=None)
    def parse(file: UploadFile = File(...), options: str = Form("{}")) -> dict[str, Any] | JSONResponse:
        try:
            opts = json.loads(options or "{}")
        except json.JSONDecodeError:
            return error(400, "invalid_options", "options must be a JSON object")
        if not isinstance(opts, dict):
            return error(400, "invalid_options", "options must be a JSON object")
        try:
            options_model = parse_options(opts)
        except OptionsError as exc:
            return error(400, "invalid_options", str(exc))
        content = file.file.read()
        filename = file.filename or "document"
        try:
            doc = convert(content, filename, options_model)
        except OptionsError as exc:  # the engine rejected a value in ``engine``
            return error(400, "invalid_options", str(exc))
        except ShimError as exc:
            return error(exc.status, exc.code, exc.message)
        except Exception as exc:  # docling.exceptions.ConversionError and friends
            if "format not allowed" in str(exc).lower():  # Docling's message for unsupported formats
                return error(415, "unsupported_media_type", f"{filename}: {exc}")
            return error(422, "conversion_failed", f"{filename}: {type(exc).__name__}: {exc}")
        return to_ir(doc, include_page_images=options_model.include_page_images)

    return app


def __getattr__(name: str) -> Any:
    # ``uvicorn docling_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import Docling.
    if name == "app":
        return create_app()
    raise AttributeError(name)
