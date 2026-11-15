"""Calls PaddleOCR's PP-StructureV3 and reduces each page result to plain dicts for ``convert``.

PaddleOCR is imported on first use, so importing this module needs no engine. Each distinct
combination of options builds (and keeps) its own pipeline, because PaddleX only loads the models
for the features that are on when the pipeline is built: a request can turn a feature off, but it
cannot turn on one the pipeline was built without.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from shimkit import ShimError, check_engine_options, temp_upload

# PaddleX reads PDFs and common raster images (paddlex/inference/common/reader).
SUPPORTED_EXTENSIONS = frozenset({"pdf", "png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"})

# shim option -> PP-StructureV3 argument (used for both the constructor and ``predict``)
OPTION_ARGUMENTS = {
    "tables": "use_table_recognition",
    "formulas": "use_formula_recognition",
    "charts": "use_chart_recognition",
    "seals": "use_seal_recognition",
    "orientation": "use_doc_orientation_classify",
    "unwarp": "use_doc_unwarping",
}


# Pipelines kept at once: each holds its models in memory.
MAX_PIPELINES = 4

# shim option -> the argument it sets, when it is not a plain on/off feature (see OPTION_ARGUMENTS)
OPTIONAL_FLAG_ARGUMENTS = {
    "textline_orientation": "use_textline_orientation",
    "region_detection": "use_region_detection",
}

# An argument that has a named option: ``engine`` may not set it.
NAMED_ARGUMENTS = {
    **{argument: option for option, argument in OPTION_ARGUMENTS.items()},
    **{argument: option for option, argument in OPTIONAL_FLAG_ARGUMENTS.items()},
    "lang": "languages",
    "ocr_version": "ocr_version",
}

# What ``engine`` may set: the arguments of ``PPStructureV3.__init__`` and ``.predict`` (PaddleOCR
# 3.7.0, read from ``inspect.signature``) minus the named options above and minus the operational
# ones (model directories and batch sizes: see shimkit.DENIED_ENGINE_KEYS). The live test
# ``test_engine_parameters_accounted_for`` fails when a PaddleOCR upgrade adds or removes an argument.
ENGINE_INIT_ARGUMENTS = frozenset(
    {
        "layout_detection_model_name", "layout_threshold", "layout_nms", "layout_unclip_ratio",
        "layout_merge_bboxes_mode", "chart_recognition_model_name", "region_detection_model_name",
        "doc_orientation_classify_model_name", "doc_unwarping_model_name", "text_detection_model_name",
        "text_det_limit_side_len", "text_det_limit_type", "text_det_thresh", "text_det_box_thresh",
        "text_det_unclip_ratio", "textline_orientation_model_name", "text_recognition_model_name",
        "text_rec_score_thresh", "table_classification_model_name",
        "wired_table_structure_recognition_model_name", "wireless_table_structure_recognition_model_name",
        "wired_table_cells_detection_model_name", "wireless_table_cells_detection_model_name",
        "table_orientation_classify_model_name", "seal_text_detection_model_name", "seal_det_limit_side_len",
        "seal_det_limit_type", "seal_det_thresh", "seal_det_box_thresh", "seal_det_unclip_ratio",
        "seal_text_recognition_model_name", "seal_rec_score_thresh", "formula_recognition_model_name",
        "format_block_content", "markdown_ignore_labels",
    }
)  # fmt: skip
ENGINE_PREDICT_ARGUMENTS = frozenset(
    {
        "format_block_content", "layout_threshold", "layout_nms", "layout_unclip_ratio",
        "layout_merge_bboxes_mode", "text_det_limit_side_len", "text_det_limit_type", "text_det_thresh",
        "text_det_box_thresh", "text_det_unclip_ratio", "text_rec_score_thresh", "seal_det_limit_side_len",
        "seal_det_limit_type", "seal_det_thresh", "seal_det_box_thresh", "seal_det_unclip_ratio",
        "seal_rec_score_thresh", "use_wired_table_cells_trans_to_html", "use_wireless_table_cells_trans_to_html",
        "use_table_orientation_classify", "use_ocr_results_with_table_cells", "use_e2e_wired_table_rec_model",
        "use_e2e_wireless_table_rec_model", "markdown_ignore_labels",
    }
)  # fmt: skip
# Arguments of those two signatures that no option reaches, and why. Besides these, every
# ``*_model_dir`` and ``*_batch_size`` argument is refused by shimkit's denylist (operational).
NOT_EXPOSED = {
    "input": "the document itself",
}


def split_engine(engine: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ``engine`` option as ``(constructor arguments, predict arguments)``; a key both accept goes
    to both. ``400 invalid_options`` for a denied key, one a named option covers, or an unknown one."""
    if not engine:
        return {}, {}
    check_engine_options(engine)
    init: dict[str, Any] = {}
    predict: dict[str, Any] = {}
    for key, value in engine.items():
        if key in NAMED_ARGUMENTS:
            raise ShimError(
                400, "invalid_options", f"option 'engine' sets {key!r}; use the option {NAMED_ARGUMENTS[key]!r} instead"
            )
        if key not in ENGINE_INIT_ARGUMENTS and key not in ENGINE_PREDICT_ARGUMENTS:
            raise ShimError(400, "invalid_options", f"option 'engine' has no PP-StructureV3 argument {key!r}")
        if key in ENGINE_INIT_ARGUMENTS:
            init[key] = value
        if key in ENGINE_PREDICT_ARGUMENTS:
            predict[key] = value
    return init, predict


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


class PaddleEngine:
    """``(content, filename, options) -> list of reduced pages``. Not thread-safe: the app
    serialises calls. ``options`` holds the keys of ``OPTION_ARGUMENTS`` and
    ``OPTIONAL_FLAG_ARGUMENTS`` plus ``heading_levels``, ``languages``, ``ocr_version`` and ``engine``."""

    def __init__(self, lang: str | None = None) -> None:
        self.lang = lang
        self._pipelines: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def _pipeline(self, init: dict[str, Any]) -> Any:
        key = json.dumps(init, sort_keys=True, default=str)
        with self._lock:
            if key in self._pipelines:
                self._pipelines.move_to_end(key)
                return self._pipelines[key]
            try:
                from paddleocr import PPStructureV3

                pipeline = PPStructureV3(**init)
            except Exception as exc:  # missing PaddlePaddle, model download failure, bad lang, ...
                message = f"cannot start PP-StructureV3: {type(exc).__name__}: {exc}"
                raise ShimError(503, "engine_unavailable", message) from exc
            self._pipelines[key] = pipeline
            while len(self._pipelines) > MAX_PIPELINES:
                self._pipelines.popitem(last=False)
            return pipeline

    def __call__(self, content: bytes, filename: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        flags: dict[str, Any] = {argument: options[name] for name, argument in OPTION_ARGUMENTS.items()}
        for name, argument in OPTIONAL_FLAG_ARGUMENTS.items():
            if options.get(name) is not None:
                flags[argument] = options[name]
        engine_init, engine_predict = split_engine(options.get("engine"))
        init: dict[str, Any] = {**engine_init, **flags}
        languages = options.get("languages")
        if languages:
            init["lang"] = languages[0]
        elif self.lang:
            init["lang"] = self.lang
        if options.get("ocr_version"):
            init["ocr_version"] = options["ocr_version"]
        pipeline = self._pipeline(init)
        with temp_upload(content, filename) as path:
            results = list(pipeline.predict(str(path), **{**engine_predict, **flags}))
        if options.get("heading_levels"):
            apply_heading_levels(results)
        return [reduce_page(result, position) for position, result in enumerate(results, start=1)]
