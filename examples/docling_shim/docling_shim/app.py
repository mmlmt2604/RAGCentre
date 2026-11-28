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
import sys
import threading
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from shimkit import ShimError, boolean, choice, integer, page_range, parse_options, strings
from shimkit import create_app as create_shim_app

from docling_shim.convert import to_ir
from docling_shim.remote import DoclingServeConverter

SHIM_VERSION = "0.3.1"
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
    "image/gif",
]
OCR_ENGINES = ["auto", "easyocr", "tesseract", "rapidocr", "ocrmac"]
OPTIONS = [
    boolean("ocr", True, "Run OCR on scanned pages and images in PDFs"),
    boolean("include_page_images", False, "Render each page and return it as a base64 PNG in pages[].image"),
    boolean(
        "heading_hierarchy",
        True,
        "Rank section headers by PDF bookmarks, numbering and font style (PDF only); "
        "without it Docling puts every header at one level",
    ),
    choice(
        "ocr_engine",
        OCR_ENGINES,
        "auto",
        "The OCR engine: auto picks one that is installed. tesseract needs the tesseract program on PATH",
    ),
    strings(
        "ocr_lang",
        "OCR languages in the engine's own codes (EasyOCR: en, de; Tesseract: eng, deu). Default: the engine's",
    ),
    boolean(
        "force_full_page_ocr", False, "OCR every page as an image instead of only the regions without a text layer"
    ),
    choice(
        "table_mode",
        ["accurate", "fast"],
        "accurate",
        "TableFormer mode: accurate is slower and reads complex tables better",
    ),
    boolean("code_enrichment", False, "Recognise the language of code blocks and read them with a code model (slow)"),
    boolean("formula_enrichment", False, "Read formulas as LaTeX with a formula model (slow)"),
    boolean(
        "picture_classification",
        False,
        "Classify each picture (chart, photo, logo...); the class goes to metadata.classification",
    ),
    boolean(
        "picture_description",
        False,
        "Describe each picture with a small local vision model (slow); the text is added to the figure",
    ),
    page_range("page_range", "Only convert these pages: [first, last], 1-based and inclusive"),
    integer(
        "max_pages",
        None,
        minimum=1,
        maximum=100000,
        description="Convert at most this many pages from the start (or from page_range's first page)",
    ),
]

# (content, filename, options) -> DoclingDocument. ``options`` holds every validated option in ``OPTIONS``.
Converter = Callable[[bytes, str, dict[str, Any]], Any]


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


def _page_arguments(options: dict[str, Any]) -> dict[str, Any]:
    """``convert()``'s page window from ``page_range`` and ``max_pages`` (1-based, inclusive)."""
    first, last = options["page_range"] or (1, sys.maxsize)
    if options["max_pages"] is not None:
        last = min(last, first + options["max_pages"] - 1)
    return {"page_range": (first, last)} if (first, last) != (1, sys.maxsize) else {}


def _ocr_options(engine: str, languages: list[str] | None, full_page: bool) -> Any:
    from docling.datamodel import pipeline_options as po

    classes = {
        "auto": po.OcrAutoOptions,
        "easyocr": po.EasyOcrOptions,
        "tesseract": po.TesseractCliOcrOptions,
        "rapidocr": po.RapidOcrOptions,
        "ocrmac": po.OcrMacOptions,
    }
    kwargs: dict[str, Any] = {}
    if languages:
        kwargs["lang"] = languages
    if full_page:
        kwargs["mode"] = po.OcrMode.FULL_PAGE
    return classes[engine](**kwargs)


class DoclingConverter:
    """Builds and caches one ``DocumentConverter`` per combination of converter-level options."""

    # the options that change how the pipeline is built; page_range and max_pages are per call
    PIPELINE_OPTIONS = tuple(o.key for o in OPTIONS if o.key not in ("page_range", "max_pages"))

    def __init__(self) -> None:
        self._converters: dict[tuple[Any, ...], Any] = {}
        self._lock = threading.Lock()

    def _converter(self, options: dict[str, Any]) -> Any:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

        key = tuple(tuple(v) if isinstance(v, list) else v for v in (options[k] for k in self.PIPELINE_OPTIONS))
        with self._lock:
            if key not in self._converters:
                pipeline = PdfPipelineOptions()
                pipeline.do_ocr = options["ocr"]
                pipeline.ocr_options = _ocr_options(
                    options["ocr_engine"], options["ocr_lang"], options["force_full_page_ocr"]
                )
                pipeline.generate_page_images = options["include_page_images"]
                # Docling leaves every section header at level 1 unless this is on; without it
                # RAGCentre's heading paths and structure chunker see a flat document.
                pipeline.heading_hierarchy_options.enabled = options["heading_hierarchy"]
                pipeline.table_structure_options.mode = TableFormerMode(options["table_mode"])
                pipeline.do_code_enrichment = options["code_enrichment"]
                pipeline.do_formula_enrichment = options["formula_enrichment"]
                pipeline.do_picture_classification = options["picture_classification"]
                pipeline.do_picture_description = options["picture_description"]
                self._converters[key] = DocumentConverter(
                    format_options={
                        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline),
                        InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline),
                    }
                )
            return self._converters[key]

    def __call__(self, content: bytes, filename: str, options: dict[str, Any]) -> Any:
        from docling.datamodel.base_models import DocumentStream

        stream = DocumentStream(name=filename, stream=io.BytesIO(content))
        return self._converter(options).convert(stream, **_page_arguments(options)).document


def create_app(converter: Converter | None = None) -> FastAPI:
    remote = converter if isinstance(converter, DoclingServeConverter) else None
    if converter is None:
        remote = DoclingServeConverter.from_env()
    convert = converter or remote or DoclingConverter()

    def parse(content: bytes, filename: str, options: dict[str, Any]) -> dict[str, Any]:
        opts = parse_options(OPTIONS, options)
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
        options=OPTIONS,
        api_key_env="DOCLING_SHIM_API_KEY",
        parse=parse,
        engine_check=(lambda: remote.reachable) if remote is not None else None,
        jobs=True,  # a scanned PDF can take longer than one request may last
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn docling_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import Docling.
    if name == "app":
        return create_app()
    raise AttributeError(name)
