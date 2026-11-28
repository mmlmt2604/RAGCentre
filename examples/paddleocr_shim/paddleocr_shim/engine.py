"""Calls PaddleOCR's PP-StructureV3 and reduces each page result to plain dicts for ``convert``.

PaddleOCR is imported on first use, so importing this module needs no engine. PaddleX only loads
models for features that are enabled when a pipeline is built.
"""

from __future__ import annotations

import gc
import os
from typing import Any

from shimkit import ShimError, temp_upload

# PaddleX reads PDFs and common raster images (paddlex/inference/common/reader).
SUPPORTED_EXTENSIONS = frozenset({"pdf", "png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"})

# shim option -> PP-StructureV3 argument (used for both the constructor and ``predict``). An option
# left unset (None) is not passed, so the engine's own default applies.
OPTION_ARGUMENTS = {
    "tables": "use_table_recognition",
    "formulas": "use_formula_recognition",
    "charts": "use_chart_recognition",
    "seals": "use_seal_recognition",
    "orientation": "use_doc_orientation_classify",
    "unwarp": "use_doc_unwarping",
    "textline_orientation": "use_textline_orientation",
    "region_detection": "use_region_detection",
}
# the same for PaddleOCR-VL, which has no table or formula switches (it always reads them)
VL_OPTION_ARGUMENTS = {
    "charts": "use_chart_recognition",
    "seals": "use_seal_recognition",
    "orientation": "use_doc_orientation_classify",
    "unwarp": "use_doc_unwarping",
}


def apply_heading_levels(results: list[Any]) -> None:
    """Set ``title_level`` on the ``paragraph_title`` blocks, using PaddleX's own heading-level
    assignment (the one PaddleOCR-VL runs; PP-StructureV3 does not). It clusters titles by pixel
    height and numbering, so on short documents the levels can look arbitrary. Failures are ignored:
    the headings then keep their default level."""
    try:
        from paddlex.inference.pipelines.layout_parsing.title_level import assign_levels_to_parsing_res

        assign_levels_to_parsing_res([list(result["parsing_res_list"]) for result in results])
    except Exception:  # internal PaddleX API; never fail a parse over it
        return


def reduce_page(result: Any, position: int) -> dict[str, Any]:
    """One PP-StructureV3 page result -> ``{"page", "width", "height", "blocks"}``.

    ``position`` is the 1-based position in the result list, used when ``page_index`` is None."""
    page_index = result.get("page_index")
    blocks = []
    for block in result["parsing_res_list"]:
        blocks.append(
            {
                "label": block.label,
                "content": block.content,
                "bbox": list(block.bbox),
                "order": block.order_index,
                "title_level": getattr(block, "title_level", None),
            }
        )
    return {
        "page": page_index + 1 if page_index is not None else position,
        "width": result["width"],
        "height": result["height"],
        "blocks": blocks,
    }


def _number(value: Any) -> Any:
    """A page size that PaddleOCR-VL sometimes wraps in a one-item list."""
    return value[0] if isinstance(value, list | tuple) else value


def reduce_vl_page(result: Any, position: int) -> dict[str, Any]:
    """One PaddleOCR-VL page result (after ``restructure_pages``) -> the same reduced page.

    VL blocks have no ``order_index``: the list is already in reading order. ``title_level`` was
    set by ``restructure_pages(relevel_titles=True)``."""
    page_index = result.get("page_index")
    blocks = [
        {
            "label": block.label,
            "content": block.content,
            "bbox": list(block.bbox),
            "order": order,
            "title_level": getattr(block, "title_level", None),
        }
        for order, block in enumerate(result["parsing_res_list"], start=1)
    ]
    return {
        "page": page_index + 1 if isinstance(page_index, int) else position,
        "width": _number(result["width"]),
        "height": _number(result["height"]),
        "blocks": blocks,
    }


def _vl_settings() -> dict[str, Any]:
    """PaddleOCR-VL's constructor settings from the environment: the recognition backend (``native``
    runs the model here; ``vllm-server``, ``sglang-server``, ``llama-cpp-server``... call one you
    run), its URL, and the pipeline version."""
    env = os.environ.get
    settings: dict[str, Any] = {}
    for variable, argument in (
        ("PADDLEOCR_VL_VERSION", "pipeline_version"),
        ("PADDLEOCR_VL_BACKEND", "vl_rec_backend"),
        ("PADDLEOCR_VL_SERVER_URL", "vl_rec_server_url"),
        ("PADDLEOCR_VL_API_MODEL", "vl_rec_api_model_name"),
        ("PADDLEOCR_VL_API_KEY", "vl_rec_api_key"),
    ):
        if env(variable):
            settings[argument] = env(variable)
    return settings


class PaddleEngine:
    """``(content, filename, options) -> list of reduced pages``. Not thread-safe: the app
    serialises calls. ``options`` holds the shim's validated options; ``pipeline`` chooses
    PP-StructureV3 (``structure_v3``) or PaddleOCR-VL (``vl``)."""

    def __init__(self, lang: str | None = None) -> None:
        self.lang = lang
        self._pipelines: dict[tuple[Any, ...], Any] = {}

    def _structure_pipeline(self, flags: dict[str, bool], lang: str | None, limit: int | None) -> Any:
        key = ("structure_v3", tuple(sorted(flags.items())), lang, limit)
        if key not in self._pipelines:
            if self._pipelines:
                self._pipelines.clear()
                gc.collect()
            try:
                from paddleocr import PPStructureV3

                kwargs: dict[str, Any] = dict(flags)
                if lang:
                    kwargs["lang"] = lang
                if limit is not None:
                    kwargs["text_det_limit_side_len"] = limit
                self._pipelines[key] = PPStructureV3(**kwargs)
            except Exception as exc:  # missing PaddlePaddle, model download failure, bad lang, ...
                message = f"cannot start PP-StructureV3: {type(exc).__name__}: {exc}"
                raise ShimError(503, "engine_unavailable", message) from exc
        return self._pipelines[key]

    def _vl_pipeline(self, flags: dict[str, bool]) -> Any:
        key = ("vl", tuple(sorted(flags.items())))
        if key not in self._pipelines:
            if self._pipelines:
                self._pipelines.clear()
                gc.collect()
            try:
                from paddleocr import PaddleOCRVL

                self._pipelines[key] = PaddleOCRVL(**_vl_settings(), **flags)
            except Exception as exc:
                message = f"cannot start PaddleOCR-VL: {type(exc).__name__}: {exc}"
                raise ShimError(503, "engine_unavailable", message) from exc
        return self._pipelines[key]

    def __call__(self, content: bytes, filename: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        if options.get("pipeline") == "vl":
            return self._run_vl(content, filename, options)
        flags = {
            argument: options[name] for name, argument in OPTION_ARGUMENTS.items() if options.get(name) is not None
        }
        limit = options.get("text_det_limit_side_len")
        lang = options.get("lang") or self.lang
        pipeline = self._structure_pipeline(flags, lang, limit)
        predict = dict(flags) if limit is None else {**flags, "text_det_limit_side_len": limit}
        with temp_upload(content, filename) as path:
            results = list(pipeline.predict(str(path), **predict))
        if options.get("heading_levels"):
            apply_heading_levels(results)
        return [reduce_page(result, position) for position, result in enumerate(results, start=1)]

    def _run_vl(self, content: bytes, filename: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        flags = {
            argument: options[name] for name, argument in VL_OPTION_ARGUMENTS.items() if options.get(name) is not None
        }
        pipeline = self._vl_pipeline(flags)
        with temp_upload(content, filename) as path:
            results = list(pipeline.predict(str(path), **flags))
        # Merge tables that continue across pages and give section titles their levels.
        results = pipeline.restructure_pages(results, merge_tables=True, relevel_titles=True)
        return [reduce_vl_page(result, position) for position, result in enumerate(results, start=1)]
