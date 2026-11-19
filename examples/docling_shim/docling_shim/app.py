"""A ``ragcentre.parser/v1`` service that parses documents with Docling.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route), all booleans:

    ocr                  run OCR on scanned pages (default true)
    include_page_images  return each page as an image (default false)
    heading_hierarchy    rank section headers by bookmarks, numbering and font style (default
                         true; Docling's own default leaves every header at the same level)

Run it with::

    uvicorn docling_shim.app:app --port 8701

Set ``DOCLING_SERVE_URL`` to use an existing docling-serve instead of running Docling here (see
``remote.py``). Set ``DOCLING_SHIM_API_KEY`` to require ``Authorization: Bearer <key>`` on every request except
``GET /info`` (give RAGCentre the same value as the provider's ``api_key`` secret).
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from shimkit import ShimError, bool_option
from shimkit import create_app as create_shim_app

from docling_shim.convert import to_ir
from docling_shim.remote import DoclingServeConverter

SHIM_VERSION = "0.2.0"
NAME = "docling-shim"
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
DEFAULTS = {"ocr": True, "include_page_images": False, "heading_hierarchy": True}

# (content, filename, options) -> DoclingDocument. ``options`` holds the validated booleans in
# ``DEFAULTS``.
Converter = Callable[[bytes, str, dict[str, bool]], Any]


def _docling_version() -> str:
    try:
        return version("docling")
    except PackageNotFoundError:
        return "unknown"


def service_version(remote: DoclingServeConverter | None = None) -> str:
    # RAGCentre pins this when the provider is created; a Docling upgrade changes parse output,
    # so it is part of the version. With a remote docling-serve it is the server's Docling version.
    if remote is not None:
        return f"{SHIM_VERSION}+docling-serve.{remote.server_version()}"
    return f"{SHIM_VERSION}+docling.{_docling_version()}"


class DoclingConverter:
    """Builds and caches one ``DocumentConverter`` per option combination."""

    def __init__(self) -> None:
        self._converters: dict[tuple[bool, bool, bool], Any] = {}
        self._lock = threading.Lock()

    def _converter(self, ocr: bool, images: bool, hierarchy: bool) -> Any:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        with self._lock:
            key = (ocr, images, hierarchy)
            if key not in self._converters:
                options = PdfPipelineOptions()
                options.do_ocr = ocr
                options.generate_page_images = images
                # Docling leaves every section header at level 1 unless this is on; without it
                # RAGCentre's heading paths and structure chunker see a flat document.
                options.heading_hierarchy_options.enabled = hierarchy
                self._converters[key] = DocumentConverter(
                    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
                )
            return self._converters[key]

    def __call__(self, content: bytes, filename: str, options: dict[str, bool]) -> Any:
        from docling.datamodel.base_models import DocumentStream

        converter = self._converter(options["ocr"], options["include_page_images"], options["heading_hierarchy"])
        return converter.convert(DocumentStream(name=filename, stream=io.BytesIO(content))).document


def create_app(converter: Converter | None = None) -> FastAPI:
    remote = converter if isinstance(converter, DoclingServeConverter) else None
    if converter is None:
        remote = DoclingServeConverter.from_env()
    convert = converter or remote or DoclingConverter()

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        opts = {name: bool_option(options, name, default) for name, default in DEFAULTS.items()}
        try:
            doc = convert(content, filename, opts)
        except Exception as exc:  # docling.exceptions.ConversionError and friends
            if "format not allowed" in str(exc).lower():  # Docling's message for unsupported formats
                raise ShimError(415, "unsupported_media_type", f"{filename}: {exc}") from exc
            raise
        return to_ir(doc, include_page_images=opts["include_page_images"])

    return create_shim_app(
        name=NAME,
        version=lambda: service_version(remote),
        mime_types=MIME_TYPES,
        capabilities={"page_images": True},
        api_key_env="DOCLING_SHIM_API_KEY",
        parse=parse,
        jobs=True,  # a scanned PDF can take longer than one request may last
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn docling_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import Docling.
    if name == "app":
        return create_app()
    raise AttributeError(name)
