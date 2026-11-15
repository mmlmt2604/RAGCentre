"""A ``ragcentre.parser/v1`` service that parses documents with Docling.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route), both booleans; any other key is ``400 invalid_options``:

    ocr                   run OCR (default true)
    include_page_images   return each rendered page as a base64 PNG (default false)

They apply to PDFs and to image files. Run it with::

    uvicorn docling_shim.app:app --port 8701

Set ``DOCLING_SHIM_API_KEY`` to require ``Authorization: Bearer <key>`` (give RAGCentre the same
value as the provider's ``api_key`` secret). ``GET /info`` stays open: RAGCentre reads it without
credentials when it creates the provider.
"""

from __future__ import annotations

import hmac
import importlib.util
import io
import json
import os
import platform
import re
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import ErrorDetails

from docling_shim.convert import to_ir

SHIM_VERSION = "0.2.0"
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


class Options(BaseModel):
    """The parse route's options. Unknown keys are rejected and types are strict, so ``"false"`` is
    not a boolean."""

    model_config = ConfigDict(extra="forbid", strict=True)

    ocr: bool = Field(True, description="Run OCR on pages and images.")
    include_page_images: bool = Field(False, description="Return each rendered page as a base64 PNG.")


# (content, filename, options) -> DoclingDocument
Converter = Callable[[bytes, str, Options], Any]

# Converters kept at once: each holds its models in memory.
MAX_CONVERTERS = 4


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
    # RAGCentre pins this when the provider is created. A Docling upgrade or a different automatic
    # OCR engine changes parse output, so both are part of the version.
    return f"{SHIM_VERSION}+docling.{_docling_version()}.ocr-{_suffix(resolve_ocr_engine())}"


class DoclingConverter:
    """Builds and caches one ``DocumentConverter`` per distinct set of options (the four most
    recently used)."""

    def __init__(self) -> None:
        self._converters: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def _converter(self, options: Options) -> Any:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

        key = options.model_dump_json()
        with self._lock:
            if key in self._converters:
                self._converters.move_to_end(key)
                return self._converters[key]
            pipeline = PdfPipelineOptions()
            pipeline.do_ocr = options.ocr
            pipeline.generate_page_images = options.include_page_images
            # Images run through the same pipeline as PDFs and need the same options.
            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline),
                    InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline),
                }
            )
            self._converters[key] = converter
            while len(self._converters) > MAX_CONVERTERS:
                self._converters.popitem(last=False)
            return converter

    def __call__(self, content: bytes, filename: str, options: Options) -> Any:
        from docling.datamodel.base_models import DocumentStream

        result = self._converter(options).convert(DocumentStream(name=filename, stream=io.BytesIO(content)))
        return result.document


# --- option errors ----------------------------------------------------------------------------
# The same wording as shimkit's (this shim does not import it).


def _bounds(key: str) -> dict[str, Any]:
    field = Options.model_fields.get(key)
    found: dict[str, Any] = {}
    for item in field.metadata if field else []:
        for name in ("ge", "gt", "le", "lt"):
            if hasattr(item, name):
                found[name] = getattr(item, name)
    return found


def _range(noun: str, bounds: dict[str, Any]) -> str:
    low = bounds.get("ge", bounds.get("gt"))
    high = bounds.get("le", bounds.get("lt"))
    if "ge" in bounds and "le" in bounds:
        return f"{noun} from {low} to {high}"
    parts = []
    if "ge" in bounds:
        parts.append(f"at least {low}")
    if "gt" in bounds:
        parts.append(f"greater than {low}")
    if "le" in bounds:
        parts.append(f"at most {high}")
    if "lt" in bounds:
        parts.append(f"less than {high}")
    return f"{noun} that is {' and '.join(parts)}" if parts else noun


def _expectation(error: ErrorDetails) -> str:
    kind = error["type"]
    key = str(error["loc"][0]) if error["loc"] else ""
    if kind == "bool_type":
        return "true or false"
    if kind == "literal_error":
        return str((error.get("ctx") or {}).get("expected", "one of the allowed values")).replace("'", "")
    if kind in ("int_type", "int_parsing", "int_from_float"):
        return _range("an integer", _bounds(key))
    if kind in ("float_type", "float_parsing"):
        return _range("a number", _bounds(key))
    simple = {"string_type": "a string", "list_type": "a list", "tuple_type": "a list", "dict_type": "an object"}
    return simple.get(kind, str(error["msg"]))


def options_message(exc: ValidationError) -> str:
    errors = exc.errors()
    first = errors[0]
    top = first["loc"][0] if first["loc"] else None
    key = str(top) if top is not None else "options"
    if first["type"] == "extra_forbidden":
        return f"option {key!r} is not recognised"
    same = [e for e in errors if e["loc"] and e["loc"][0] == top and e["type"] != "extra_forbidden"]
    return f"option {key!r} must be {' or '.join(dict.fromkeys(_expectation(e) for e in same))}"


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
            options_model = Options.model_validate(opts)
        except ValidationError as exc:
            return error(400, "invalid_options", options_message(exc))
        content = file.file.read()
        filename = file.filename or "document"
        try:
            doc = convert(content, filename, options_model)
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
