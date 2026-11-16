"""A ``ragcentre.parser/v1`` service that parses documents with PaddleOCR-VL (``paddleocr.PaddleOCRVL``).

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

Options (from the pipeline's parse route; ``/info`` reports them as ``options_schema``, and an unknown
or mistyped one is ``400 invalid_options``):

    layout_detection, orientation, unwarp, charts, seals, image_ocr   on/off features
    heading_levels, merge_tables   the engine's second step over the whole document
    engine                other PaddleOCRVL arguments, after a check (see ``engine.py``)

PaddleOCR-VL has no page selection, so there is no ``page_range``; and it is multilingual, so there is
no ``languages``.

Run it with::

    uvicorn paddleocr_vl_shim.app:app --port 8705

Environment: ``PADDLEOCR_VL_SHIM_API_KEY`` requires ``Authorization: Bearer <key>`` (except on
``GET /info``). Which pipeline generation runs and where its recognition model runs are service
settings, ``PADDLEOCR_VL_SHIM_PIPELINE_VERSION`` and ``PADDLEOCR_VL_SHIM_REC_*`` (see
``RecognitionSettings`` in ``engine.py`` and the README); they are part of the reported version, except
the server's URL, key and concurrency.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from pydantic import Field
from shimkit import ShimError, ShimOptions, file_extension
from shimkit import create_app as create_shim_app

from paddleocr_vl_shim.convert import to_ir
from paddleocr_vl_shim.engine import SUPPORTED_EXTENSIONS, PaddleVLEngine, RecognitionSettings, split_engine

SHIM_VERSION = "0.1.0"
NAME = "paddleocr-vl-shim"
MIME_TYPES = [
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/bmp",
    "image/tiff",
    "image/webp",
]


class Options(ShimOptions):
    layout_detection: bool = Field(
        True,
        description="Detect the page's regions first. Off, the whole page is one block of the kind "
        "engine.prompt_label names (ocr, formula, table, chart, spotting or seal; default ocr).",
    )
    orientation: bool = Field(False, description="Document orientation classification.")
    unwarp: bool = Field(False, description="Document unwarping.")
    charts: bool = Field(False, description="Recognise charts (otherwise a chart is a figure with no text).")
    seals: bool = Field(False, description="Recognise seals (otherwise a seal is a figure with no text).")
    image_ocr: bool = Field(False, description="Read the text inside pictures (otherwise a picture has no text).")
    heading_levels: bool = Field(
        True,
        description="Rank section titles with PaddleX's heading-level assignment, over the whole document "
        "(otherwise all are level 2).",
    )
    merge_tables: bool = Field(True, description="Join a table that continues across pages into one table.")
    engine: dict[str, Any] = Field(
        default_factory=dict,
        description="Other PaddleOCRVL arguments (layout thresholds, sampling settings such as temperature and "
        "max_new_tokens, prompt_label, model names), passed to its constructor and predict(). Model "
        "directories, URLs and keys are refused. See "
        "https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html",
    )


# (content, filename, options) -> reduced pages (see convert.py)
Engine = Callable[[bytes, str, dict[str, Any]], list[dict[str, Any]]]


def _paddleocr_version() -> str:
    try:
        return version("paddleocr")
    except PackageNotFoundError:
        return "unknown"


def _part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)


def service_version(settings: RecognitionSettings | None = None) -> str:
    # RAGCentre pins this when the provider is created. A PaddleOCR upgrade, the pipeline generation, the
    # backend or the recognition model change the output, so each is part of the version. The server's
    # URL, key and concurrency do not, and are never reported.
    settings = settings or RecognitionSettings.from_env()
    version_ = f"{SHIM_VERSION}+paddleocr.{_paddleocr_version()}.vl-{re.sub(r'[^A-Za-z0-9._-]', '-', settings.pipeline_version)}"
    if settings.backend != "native":
        version_ += f".backend-{_part(settings.backend)}"
    if settings.model:
        version_ += f".model-{_part(settings.model)}"
    if settings.api_model:
        version_ += f".apimodel-{_part(settings.api_model)}"
    return version_


def create_app(engine: Engine | None = None, settings: RecognitionSettings | None = None) -> FastAPI:
    settings = settings or (engine.settings if isinstance(engine, PaddleVLEngine) else RecognitionSettings.from_env())
    real = None if engine is not None else PaddleVLEngine(settings)
    run = engine or real

    def parse(content: bytes, filename: str, options: Options) -> dict[str, Any]:
        assert run is not None
        opts = options.model_dump()
        split_engine(options.engine)  # a denied or unknown key is a 400 whatever the engine
        if file_extension(filename) not in SUPPORTED_EXTENSIONS:
            raise ShimError(415, "unsupported_media_type", f"{filename}: PaddleOCR-VL reads PDF and image files only")
        try:
            ir: dict[str, Any] = to_ir(run(content, filename, opts), source_name=filename)
            return ir
        except Exception as exc:
            # Backend exceptions can echo credentials and URLs, including validation errors.
            message = str(exc)
            for private in (settings.server_url, settings.api_key):
                if private:
                    message = message.replace(private, "[redacted]")
            if isinstance(exc, ShimError):
                raise ShimError(exc.status, exc.code, message) from None
            raise ShimError(422, "conversion_failed", f"{filename}: {type(exc).__name__}: {message}") from None

    return create_shim_app(
        name=NAME,
        version=lambda: service_version(settings),
        mime_types=MIME_TYPES,
        capabilities={"page_images": False},
        api_key_env="PADDLEOCR_VL_SHIM_API_KEY",
        parse=parse,
        options_model=Options,
        serialise=True,  # PaddlePaddle pipelines are not safe to call concurrently
        shutdown=real.close if real is not None else None,
    )


def __getattr__(name: str) -> Any:
    # ``uvicorn paddleocr_vl_shim.app:app`` builds the real app lazily, so importing this module (in
    # tests) does not import PaddleOCR.
    if name == "app":
        return create_app()
    raise AttributeError(name)
