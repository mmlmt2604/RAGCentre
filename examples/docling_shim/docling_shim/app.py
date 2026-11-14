"""A ``ragcentre.parser/v1`` service that parses documents with Docling.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route): ``ocr`` (bool, default true) and
``include_page_images`` (bool, default false). Run it with::

    uvicorn docling_shim.app:app --port 8701

Set ``DOCLING_SHIM_API_KEY`` to require ``Authorization: Bearer <key>`` (give RAGCentre the same
value as the provider's ``api_key`` secret).
"""

from __future__ import annotations

import hmac
import io
import json
import os
import threading
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from docling_shim.convert import to_ir

SHIM_VERSION = "0.1.0"
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

# (content, filename, ocr, include_page_images) -> DoclingDocument
Converter = Callable[[bytes, str, bool, bool], Any]


def _docling_version() -> str:
    try:
        return version("docling")
    except PackageNotFoundError:
        return "unknown"


def service_version() -> str:
    # RAGCentre pins this when the provider is created; a Docling upgrade changes parse output,
    # so it is part of the version.
    return f"{SHIM_VERSION}+docling.{_docling_version()}"


class DoclingConverter:
    """Builds and caches one ``DocumentConverter`` per option combination."""

    def __init__(self) -> None:
        self._converters: dict[tuple[bool, bool], Any] = {}
        self._lock = threading.Lock()

    def _converter(self, ocr: bool, images: bool) -> Any:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        with self._lock:
            key = (ocr, images)
            if key not in self._converters:
                options = PdfPipelineOptions()
                options.do_ocr = ocr
                options.generate_page_images = images
                self._converters[key] = DocumentConverter(
                    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
                )
            return self._converters[key]

    def __call__(self, content: bytes, filename: str, ocr: bool, images: bool) -> Any:
        from docling.datamodel.base_models import DocumentStream

        result = self._converter(ocr, images).convert(DocumentStream(name=filename, stream=io.BytesIO(content)))
        return result.document


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(converter: Converter | None = None) -> FastAPI:
    convert = converter or DoclingConverter()
    api_key = os.environ.get("DOCLING_SHIM_API_KEY") or None
    app = FastAPI(title="Docling parser shim for RAGCentre", version=SHIM_VERSION)

    @app.middleware("http")
    async def check_key(request: Request, call_next: Any) -> Any:
        if api_key is not None:
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
        }

    @app.post("/parse", response_model=None)
    def parse(file: UploadFile = File(...), options: str = Form("{}")) -> dict[str, Any] | JSONResponse:
        try:
            opts = json.loads(options or "{}")
        except json.JSONDecodeError:
            return error(400, "invalid_options", "options must be a JSON object")
        if not isinstance(opts, dict):
            return error(400, "invalid_options", "options must be a JSON object")
        ocr = bool(opts.get("ocr", True))
        images = bool(opts.get("include_page_images", False))
        content = file.file.read()
        filename = file.filename or "document"
        try:
            doc = convert(content, filename, ocr, images)
        except Exception as exc:  # docling.exceptions.ConversionError and friends
            if "format not allowed" in str(exc).lower():  # Docling's message for unsupported formats
                return error(415, "unsupported_media_type", f"{filename}: {exc}")
            return error(422, "conversion_failed", f"{filename}: {type(exc).__name__}: {exc}")
        return to_ir(doc, include_page_images=images)

    return app


def __getattr__(name: str) -> Any:
    # ``uvicorn docling_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import Docling.
    if name == "app":
        return create_app()
    raise AttributeError(name)
