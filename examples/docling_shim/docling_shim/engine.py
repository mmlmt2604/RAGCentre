"""Builds Docling's pipeline options from the parse route's options and runs the conversion.

Docling is imported on first use, so importing this module needs no engine. One ``DocumentConverter``
is kept per distinct set of options (the four most recently used): each holds models in memory.
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from docling_shim.options import Options, OptionsError

# Converters kept at once.
MAX_CONVERTERS = 4


class ShimError(Exception):
    """A failure with a protocol error response: HTTP ``status``, ``code`` and ``message``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Settings:
    """Service settings, read from the environment at start-up. They are not options: options are
    stored in pipelines and hashed into fingerprints, and these are URLs, keys and hardware."""

    picture_description_url: str = ""
    picture_description_model: str = ""
    picture_description_api_key: str = ""
    device: str = ""
    num_threads: int | None = None

    @classmethod
    def from_env(cls) -> Settings:
        threads = os.environ.get("DOCLING_SHIM_NUM_THREADS", "").strip()
        return cls(
            picture_description_url=os.environ.get("DOCLING_SHIM_PICTURE_DESCRIPTION_URL", "").strip(),
            picture_description_model=os.environ.get("DOCLING_SHIM_PICTURE_DESCRIPTION_MODEL", "").strip(),
            picture_description_api_key=os.environ.get("DOCLING_SHIM_PICTURE_DESCRIPTION_API_KEY", "").strip(),
            device=os.environ.get("DOCLING_SHIM_DEVICE", "").strip(),
            num_threads=int(threads) if threads.isdigit() and int(threads) > 0 else None,
        )


# ``ocr_engine`` -> (Docling class name, what must be installed for it)
_OCR_CLASSES = {
    "auto": ("OcrAutoOptions", ()),
    "easyocr": ("EasyOcrOptions", ("easyocr",)),
    "rapidocr": ("RapidOcrOptions", ("rapidocr",)),  # plus its backend, see ``_RAPIDOCR_BACKENDS``
    "tesseract": ("TesseractOcrOptions", ("tesserocr",)),
    "tesseract_cli": ("TesseractCliOcrOptions", ()),
}


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# RapidOcrOptions.backend -> the package it needs (onnxruntime is the default backend)
_RAPIDOCR_BACKENDS = {"onnxruntime": "onnxruntime", "openvino": "openvino", "paddle": "paddle", "torch": "torch"}


def check_ocr_engine_available(name: str, engine: dict[str, Any] | None = None) -> None:
    """``503 engine_unavailable`` naming the missing package or program."""
    needed = list(_OCR_CLASSES[name][1])
    if name == "rapidocr":
        backend = ((engine or {}).get("ocr_options") or {}).get("backend", "onnxruntime")
        needed.append(_RAPIDOCR_BACKENDS.get(str(backend), "onnxruntime"))
    for module in needed:
        if not _installed(module):
            raise ShimError(
                503,
                "engine_unavailable",
                f"ocr_engine {name!r} needs the Python package {module!r}, which is not installed",
            )
    if name == "tesseract_cli" and shutil.which("tesseract") is None:
        raise ShimError(503, "engine_unavailable", "ocr_engine 'tesseract_cli' needs the 'tesseract' program on PATH")


def _merge(model: BaseModel, overrides: dict[str, Any], where: str = "") -> BaseModel:
    """``model`` with ``overrides`` applied, recursing into nested models and plain dicts. A key that
    is not a field is an error. The result is validated again; nested models keep their own class
    (the OCR engine the named options picked)."""
    fields = type(model).model_fields
    data: dict[str, Any] = {name: getattr(model, name) for name in fields}
    for key, value in overrides.items():
        if key not in fields:
            raise OptionsError(f"option 'engine' has no field {where + key!r}")
        current = data[key]
        if isinstance(current, BaseModel) and isinstance(value, dict):
            data[key] = _merge(current, value, f"{where}{key}.")
        elif isinstance(current, dict) and isinstance(value, dict):
            data[key] = {**current, **value}
        else:
            data[key] = value
    try:
        return type(model).model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"])
        raise OptionsError(f"option 'engine' is invalid at {where + loc!r}: {first['msg']}") from None


def _common_kwargs(options: Options, settings: Settings, po: Any) -> dict[str, Any]:
    """What the standard and the vlm pipeline have in common (``VlmPipelineOptions`` has these fields too)."""
    kwargs: dict[str, Any] = {
        "generate_page_images": options.include_page_images,
        "do_picture_classification": options.picture_classification,
        "do_picture_description": options.picture_description,
        "do_chart_extraction": options.charts,
        "images_scale": options.images_scale,
    }
    if settings.device or settings.num_threads:
        accelerator: dict[str, Any] = {}
        if settings.device:
            accelerator["device"] = settings.device
        if settings.num_threads:
            accelerator["num_threads"] = settings.num_threads
        kwargs["accelerator_options"] = po.AcceleratorOptions(**accelerator)
    if options.picture_description and settings.picture_description_url:
        api: dict[str, Any] = {"url": settings.picture_description_url}
        if settings.picture_description_model:
            api["params"] = {"model": settings.picture_description_model}
        if settings.picture_description_api_key:
            api["headers"] = {"Authorization": f"Bearer {settings.picture_description_api_key}"}
        kwargs["picture_description_options"] = po.PictureDescriptionApiOptions(**api)
        kwargs["enable_remote_services"] = True
    return kwargs


def _build_vlm_options(options: Options, settings: Settings) -> Any:
    """``VlmPipelineOptions``: the preset of ``vlm_model`` plus the options both pipelines share, with
    ``engine`` merged over them."""
    from docling.datamodel import pipeline_options as po

    kwargs = _common_kwargs(options, settings, po)
    kwargs["vlm_options"] = po.VlmConvertOptions.from_preset(options.vlm_model)
    pipeline = po.VlmPipelineOptions(**kwargs)
    if options.engine:
        merged = _merge(pipeline, options.engine)
        assert isinstance(merged, po.VlmPipelineOptions)
        return merged
    return pipeline


def build_pipeline_options(options: Options, settings: Settings | None = None) -> Any:
    """Docling's ``PdfPipelineOptions`` for these options: the named options first, then ``engine``
    merged over them. Used for PDFs and for images (both run Docling's standard PDF pipeline)."""
    from docling.datamodel import pipeline_options as po

    settings = settings or Settings()
    if options.pipeline == "vlm":
        return _build_vlm_options(options, settings)
    if options.ocr:
        check_ocr_engine_available(options.ocr_engine, options.engine)

    ocr_class = getattr(po, _OCR_CLASSES[options.ocr_engine][0])
    ocr_kwargs: dict[str, Any] = {}
    if options.languages is not None:
        ocr_kwargs["lang"] = options.languages
    if options.force_full_page_ocr:
        ocr_kwargs["mode"] = po.OcrMode.FULL_PAGE

    kwargs: dict[str, Any] = {
        "do_ocr": options.ocr,
        "generate_page_images": options.include_page_images,
        "do_table_structure": options.tables,
        "table_structure_options": po.TableStructureOptions(mode=po.TableFormerMode(options.table_mode)),
        "ocr_options": ocr_class(**ocr_kwargs),
        "do_formula_enrichment": options.formulas,
        "do_code_enrichment": options.code,
        **_common_kwargs(options, settings, po),
    }

    pipeline = po.PdfPipelineOptions(**kwargs)
    if options.engine:
        merged = _merge(pipeline, options.engine)
        assert isinstance(merged, po.PdfPipelineOptions)
        return merged
    return pipeline


class DoclingConverter:
    """``(content, filename, options) -> DoclingDocument``."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self._converters: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def _converter(self, options: Options) -> Any:
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

        # Only what reaches Docling's constructors decides the converter: not the page range.
        key = options.model_dump_json(exclude={"page_range"})
        with self._lock:
            if key in self._converters:
                self._converters.move_to_end(key)
                return self._converters[key]
            pipeline = build_pipeline_options(options, self.settings)
            # Images run through the same pipeline as PDFs and need the same options.
            extra: dict[str, Any] = {}
            if options.pipeline == "vlm":
                from docling.pipeline.vlm_pipeline import VlmPipeline

                extra["pipeline_cls"] = VlmPipeline
            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline, **extra),
                    InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline, **extra),
                }
            )
            self._converters[key] = converter
            while len(self._converters) > MAX_CONVERTERS:
                self._converters.popitem(last=False)
            return converter

    def __call__(self, content: bytes, filename: str, options: Options) -> Any:
        from docling.datamodel.base_models import DocumentStream

        extra: dict[str, Any] = {}
        if options.page_range is not None:
            extra["page_range"] = (options.page_range[0], options.page_range[1])
        stream = DocumentStream(name=filename, stream=io.BytesIO(content))
        return self._converter(options).convert(stream, **extra).document
