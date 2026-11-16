"""Calls PaddleOCR's ``PaddleOCRVL`` pipeline and reduces each page result to plain dicts for ``convert``.

PaddleOCR is imported on first use, so importing this module needs no engine. Each distinct
combination of constructor arguments builds (and keeps) its own pipeline, because PaddleX loads the
models for the features that are on when the pipeline is built.

Where the recognition model runs (the ``native`` backend, or a ``llama-cpp-server``, ``vllm-server``,
... that the service points at) is a service setting, read from the environment by
:class:`RecognitionSettings`: it changes the output, so it is part of the reported version, but the
server's URL and key are not options and are never reported.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from shimkit import ShimError, check_engine_options, temp_upload

# PaddleX reads PDFs and common raster images (paddlex/inference/common/reader).
SUPPORTED_EXTENSIONS = frozenset({"pdf", "png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"})

# Pipelines kept at once: each holds its models in memory (and, for a server backend, a client).
MAX_PIPELINES = 4

# shim option -> the PaddleOCRVL argument it sets. Each is an argument of both the constructor and
# ``predict``: a feature must be on when the pipeline is built, and can then be turned off per call.
OPTION_ARGUMENTS = {
    "orientation": "use_doc_orientation_classify",
    "unwarp": "use_doc_unwarping",
    "layout_detection": "use_layout_detection",
    "charts": "use_chart_recognition",
    "seals": "use_seal_recognition",
    "image_ocr": "use_ocr_for_image_block",
}

# Constructor arguments that are service settings (``RecognitionSettings``), never options: argument ->
# the environment variable that sets it. ``engine`` refuses them. ``shimkit``'s denylist would not
# catch ``vl_rec_server_url`` or ``vl_rec_api_key`` (it matches ``url`` and ``api_key`` exactly).
SERVICE_SETTING_ARGUMENTS = {
    "pipeline_version": "PADDLEOCR_VL_SHIM_PIPELINE_VERSION",
    "vl_rec_backend": "PADDLEOCR_VL_SHIM_REC_BACKEND",
    "vl_rec_server_url": "PADDLEOCR_VL_SHIM_REC_SERVER_URL",
    "vl_rec_model_name": "PADDLEOCR_VL_SHIM_REC_MODEL",
    "vl_rec_api_model_name": "PADDLEOCR_VL_SHIM_REC_API_MODEL",
    "vl_rec_api_key": "PADDLEOCR_VL_SHIM_REC_API_KEY",
    "vl_rec_max_concurrency": "PADDLEOCR_VL_SHIM_REC_MAX_CONCURRENCY",
}

# An argument that has a named option: ``engine`` may not set it.
NAMED_ARGUMENTS = {argument: option for option, argument in OPTION_ARGUMENTS.items()}

# What ``engine`` may set: the arguments of ``PaddleOCRVL.__init__`` and ``.predict`` (PaddleOCR 3.7.0,
# read from ``inspect.signature``) minus the named options, the service settings and the arguments in
# ``NOT_EXPOSED``. Every ``*_model_dir`` argument is refused by shimkit's denylist. The live test
# ``test_engine_parameters_accounted_for`` fails when a PaddleOCR upgrade adds or removes an argument.
# The sampling settings (``temperature``, ``top_p``, ``repetition_penalty``, ``max_new_tokens``,
# ``min_pixels``, ``max_pixels``) are here: they change the recognised text.
ENGINE_INIT_ARGUMENTS = frozenset(
    {
        "layout_detection_model_name", "layout_threshold", "layout_nms", "layout_unclip_ratio",
        "layout_merge_bboxes_mode", "doc_orientation_classify_model_name", "doc_unwarping_model_name",
        "merge_layout_blocks",
    }
)  # fmt: skip
ENGINE_PREDICT_ARGUMENTS = frozenset(
    {
        "layout_threshold", "layout_nms", "layout_unclip_ratio", "layout_merge_bboxes_mode",
        "layout_shape_mode", "merge_layout_blocks", "prompt_label", "repetition_penalty", "temperature",
        "top_p", "min_pixels", "max_pixels", "max_new_tokens", "vlm_extra_args",
    }
)  # fmt: skip
# Arguments of those two signatures that no option reaches, and why. Besides these, every
# ``*_model_dir`` argument is refused by shimkit's denylist (operational).
NOT_EXPOSED = {
    "input": "the document itself",
    "use_queues": "threading inside PaddleX, a speed setting: it does not change the output",
    "format_block_content": "only changes the result's JSON and Markdown export, which the shim does not use",
    "markdown_ignore_labels": "only changes the result's Markdown export, which the shim does not use",
}

# Backends and pipeline versions the installed PaddleOCR accepts (``paddleocr/_pipelines/paddleocr_vl.py``:
# ``_SUPPORTED_VL_BACKENDS`` and ``_AVAILABLE_PIPELINE_VERSIONS``). The live test compares them.
BACKENDS = ("native", "vllm-server", "sglang-server", "fastdeploy-server", "mlx-vlm-server", "llama-cpp-server")
PIPELINE_VERSIONS = ("v1", "v1.5", "v1.6")
# The pipeline version the shim uses when the service does not set one. It is always passed to PaddleOCR
# explicitly, so a PaddleOCR upgrade cannot change it silently (the live test compares it with the
# engine's own default).
DEFAULT_PIPELINE_VERSION = "v1.6"


@dataclass(frozen=True)
class RecognitionSettings:
    """Which PaddleOCR-VL pipeline generation runs, and where its recognition model runs. Service
    settings, read from the environment at start-up (they are deployment choices, not options):

    ``PADDLEOCR_VL_SHIM_PIPELINE_VERSION`` (``v1``, ``v1.5``, ``v1.6``), ``PADDLEOCR_VL_SHIM_REC_BACKEND``
    (``native``, or a server: ``llama-cpp-server``, ``mlx-vlm-server``, ``vllm-server``,
    ``sglang-server``, ``fastdeploy-server``), ``_REC_SERVER_URL``, ``_REC_MODEL`` (``vl_rec_model_name``),
    ``_REC_API_MODEL`` (the model name the server knows), ``_REC_API_KEY`` and ``_REC_MAX_CONCURRENCY``.
    Only the pipeline version, the backend and the model names change the output, so only they are part
    of the reported version; the URL, the key and the concurrency are not."""

    pipeline_version: str = DEFAULT_PIPELINE_VERSION
    backend: str = "native"
    server_url: str = ""
    model: str = ""
    api_model: str = ""
    api_key: str = ""
    max_concurrency: str = ""

    @classmethod
    def from_env(cls) -> RecognitionSettings:
        def read(variable: str) -> str:
            return os.environ.get(variable, "").strip()

        names = SERVICE_SETTING_ARGUMENTS
        return cls(
            pipeline_version=read(names["pipeline_version"]) or DEFAULT_PIPELINE_VERSION,
            backend=read(names["vl_rec_backend"]) or "native",
            server_url=read(names["vl_rec_server_url"]),
            model=read(names["vl_rec_model_name"]),
            api_model=read(names["vl_rec_api_model_name"]),
            api_key=read(names["vl_rec_api_key"]),
            max_concurrency=read(names["vl_rec_max_concurrency"]),
        )

    def constructor_arguments(self) -> dict[str, Any]:
        """The ``PaddleOCRVL(...)`` arguments for these settings. ``503 engine_unavailable`` naming the
        setting when one is wrong: it is the service that is misconfigured, not the request."""
        names = SERVICE_SETTING_ARGUMENTS

        def wrong(argument: str, problem: str) -> ShimError:
            return ShimError(503, "engine_unavailable", f"{names[argument]}: {problem}")

        if self.pipeline_version not in PIPELINE_VERSIONS:
            raise wrong("pipeline_version", f"{self.pipeline_version!r} is not one of {', '.join(PIPELINE_VERSIONS)}")
        arguments: dict[str, Any] = {"pipeline_version": self.pipeline_version}
        if self.backend not in BACKENDS:
            raise wrong("vl_rec_backend", f"{self.backend!r} is not one of {', '.join(BACKENDS)}")
        if self.backend != "native":
            if not self.server_url:
                raise wrong("vl_rec_server_url", f"must be set for the {self.backend!r} backend")
            arguments["vl_rec_backend"] = self.backend
            arguments["vl_rec_server_url"] = self.server_url
        elif self.server_url:
            raise wrong("vl_rec_backend", "the server URL is set but the backend is 'native'")
        if self.model:
            arguments["vl_rec_model_name"] = self.model
        if self.api_model:
            arguments["vl_rec_api_model_name"] = self.api_model
        if self.api_key:
            arguments["vl_rec_api_key"] = self.api_key
        if self.max_concurrency:
            if not self.max_concurrency.isdigit() or int(self.max_concurrency) < 1:
                raise wrong("vl_rec_max_concurrency", f"{self.max_concurrency!r} is not a positive integer")
            arguments["vl_rec_max_concurrency"] = int(self.max_concurrency)
        return arguments


def _check_service_keys(value: Any, prefix: str = "") -> None:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if key in SERVICE_SETTING_ARGUMENTS:
                raise ShimError(
                    400,
                    "invalid_options",
                    f"option 'engine' may not set {prefix + key!r}: "
                    f"it is a service setting ({SERVICE_SETTING_ARGUMENTS[key]})",
                )
            _check_service_keys(inner, f"{prefix}{key}.")
    elif isinstance(value, list):
        for inner in value:
            _check_service_keys(inner, prefix)


def split_engine(engine: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ``engine`` option as ``(constructor arguments, predict arguments)``; a key both accept goes
    to both. ``400 invalid_options`` for a denied key, a service setting, one a named option covers, or
    an unknown one."""
    if not engine:
        return {}, {}
    check_engine_options(engine)
    _check_service_keys(engine)
    init: dict[str, Any] = {}
    predict: dict[str, Any] = {}
    for key, value in engine.items():
        if key in NAMED_ARGUMENTS:
            raise ShimError(
                400, "invalid_options", f"option 'engine' sets {key!r}; use the option {NAMED_ARGUMENTS[key]!r} instead"
            )
        if key not in ENGINE_INIT_ARGUMENTS and key not in ENGINE_PREDICT_ARGUMENTS:
            raise ShimError(400, "invalid_options", f"option 'engine' has no PaddleOCRVL argument {key!r}")
        if key in ENGINE_INIT_ARGUMENTS:
            init[key] = value
        if key in ENGINE_PREDICT_ARGUMENTS:
            predict[key] = value
    return init, predict


def reduce_page(result: Any, position: int) -> dict[str, Any]:
    """One PaddleOCR-VL page result -> ``{"page", "width", "height", "blocks"}``.

    ``position`` is the 1-based position in the result list, used when ``page_index`` is None (it is
    for a single image; for a PDF it is the 0-based page). Blocks keep the engine's order."""
    page_index = result.get("page_index")
    blocks = []
    for block in result["parsing_res_list"]:
        blocks.append(
            {
                "label": block.label,
                "content": block.content,
                "bbox": list(block.bbox),
                "title_level": getattr(block, "title_level", None),
            }
        )
    return {
        "page": page_index + 1 if page_index is not None else position,
        "width": result["width"],
        "height": result["height"],
        "blocks": blocks,
    }


def server_reachable(url: str, timeout: float = 3.0) -> bool:
    """Whether a TCP connection to the recognition server's host and port succeeds.

    A failed ``predict`` does not say why: with ``use_queues`` on (PaddleOCR-VL's default) PaddleX
    re-raises a worker's error as ``RuntimeError("Exception from the 'vlm' worker: Connection error.")``,
    keeping only the message (read live against 3.7.0), so the shim asks the server instead of reading
    the exception."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


class PaddleVLEngine:
    """``(content, filename, options) -> list of reduced pages``. Not thread-safe: the app serialises
    calls. ``options`` holds the keys of ``OPTION_ARGUMENTS`` plus ``heading_levels``, ``merge_tables``
    and ``engine``."""

    def __init__(self, settings: RecognitionSettings | None = None) -> None:
        self.settings = settings or RecognitionSettings.from_env()
        self._pipelines: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def _pipeline(self, init: dict[str, Any]) -> Any:
        arguments = {**init, **self.settings.constructor_arguments()}
        key = json.dumps(init, sort_keys=True, default=str)  # the settings are fixed for this engine
        with self._lock:
            if key in self._pipelines:
                self._pipelines.move_to_end(key)
                return self._pipelines[key]
            try:
                from paddleocr import PaddleOCRVL

                pipeline = PaddleOCRVL(**arguments)
            except (TypeError, ValueError) as exc:
                if init.keys() & ENGINE_INIT_ARGUMENTS:
                    raise ShimError(400, "invalid_options", f"PaddleOCR-VL constructor: {exc}") from exc
                raise ShimError(503, "engine_unavailable", f"cannot start PaddleOCR-VL: {exc}") from exc
            except Exception as exc:  # missing PaddlePaddle, model download failure, no server, ...
                message = f"cannot start PaddleOCR-VL: {type(exc).__name__}: {exc}"
                raise ShimError(503, "engine_unavailable", message) from exc
            self._pipelines[key] = pipeline
            while len(self._pipelines) > MAX_PIPELINES:
                _, evicted = self._pipelines.popitem(last=False)
                _close(evicted)
            return pipeline

    def close(self) -> None:
        """Release every pipeline (a server backend keeps client threads)."""
        with self._lock:
            pipelines = list(self._pipelines.values())
            self._pipelines.clear()
        for pipeline in pipelines:
            _close(pipeline)

    def __call__(self, content: bytes, filename: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        flags: dict[str, Any] = {argument: options[name] for name, argument in OPTION_ARGUMENTS.items()}
        engine_init, engine_predict = split_engine(options.get("engine"))
        pipeline = self._pipeline({**engine_init, **flags})
        with temp_upload(content, filename) as path:
            try:
                results = list(pipeline.predict(str(path), **{**engine_predict, **flags}))
            except (TypeError, ValueError, AssertionError) as exc:
                raise ShimError(400, "invalid_options", f"PaddleOCR-VL predict: {exc}") from exc
            except Exception as exc:
                if self.settings.backend != "native" and not server_reachable(self.settings.server_url):
                    raise ShimError(
                        503,
                        "engine_unavailable",
                        "cannot reach the recognition server: check that it is running and that "
                        f"{SERVICE_SETTING_ARGUMENTS['vl_rec_server_url']} points at it",
                    ) from exc
                raise
        heading_levels, merge_tables = options.get("heading_levels", True), options.get("merge_tables", True)
        if results and (heading_levels or merge_tables):
            # ``predict`` leaves ``title_level`` unset and tables that run across pages in pieces; the
            # engine's own second step (``restructure_pages``) assigns heading levels over the whole
            # document and joins the pieces. It edits the blocks in place.
            results = list(
                pipeline.restructure_pages(results, merge_tables=merge_tables, relevel_titles=heading_levels)
            )
        pages = [reduce_page(result, position) for position, result in enumerate(results, start=1)]
        if merge_tables:
            # The engine empties continuation blocks and points their global_group_id at the first
            # table. Preserve all locations before build_ir drops those empty blocks.
            groups: dict[int, list[dict[str, Any]]] = {}
            for page, result in zip(pages, results, strict=True):
                for block in result["parsing_res_list"]:
                    group = getattr(block, "global_group_id", None)
                    if block.label == "table" and group is not None:
                        groups.setdefault(group, []).append({"page": page["page"], "bbox": list(block.bbox)})
            for page, result in zip(pages, results, strict=True):
                for raw, block in zip(page["blocks"], result["parsing_res_list"], strict=True):
                    group = getattr(block, "global_group_id", None)
                    spans = groups.get(group, []) if group is not None else []
                    if block.label == "table" and block.content and len(spans) > 1:
                        raw["spans"] = spans
        return pages


def _close(pipeline: Any) -> None:
    try:
        pipeline.close()
    except Exception:  # releasing is best effort; never fail a request or the shutdown over it
        return
