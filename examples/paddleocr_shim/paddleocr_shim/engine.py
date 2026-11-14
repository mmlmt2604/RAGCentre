"""Calls PaddleOCR's PP-StructureV3 and reduces each page result to plain dicts for ``convert``.

PaddleOCR is imported on first use, so importing this module needs no engine. Each distinct
combination of options builds (and keeps) its own pipeline, because PaddleX only loads the models
for the features that are on when the pipeline is built: a request can turn a feature off, but it
cannot turn on one the pipeline was built without.
"""

from __future__ import annotations

from typing import Any

from shimkit import ShimError, temp_upload

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
    serialises calls. ``options`` holds the keys of ``OPTION_ARGUMENTS`` plus ``heading_levels``."""

    def __init__(self, lang: str | None = None) -> None:
        self.lang = lang
        self._pipelines: dict[tuple[tuple[str, bool], ...], Any] = {}

    def _pipeline(self, flags: dict[str, bool]) -> Any:
        key = tuple(sorted(flags.items()))
        if key not in self._pipelines:
            try:
                from paddleocr import PPStructureV3

                kwargs: dict[str, Any] = dict(flags)
                if self.lang:
                    kwargs["lang"] = self.lang
                self._pipelines[key] = PPStructureV3(**kwargs)
            except Exception as exc:  # missing PaddlePaddle, model download failure, bad lang, ...
                message = f"cannot start PP-StructureV3: {type(exc).__name__}: {exc}"
                raise ShimError(503, "engine_unavailable", message) from exc
        return self._pipelines[key]

    def __call__(self, content: bytes, filename: str, options: dict[str, bool]) -> list[dict[str, Any]]:
        flags = {argument: options[name] for name, argument in OPTION_ARGUMENTS.items()}
        pipeline = self._pipeline(flags)
        with temp_upload(content, filename) as path:
            results = list(pipeline.predict(str(path), **flags))
        if options.get("heading_levels"):
            apply_heading_levels(results)
        return [reduce_page(result, position) for position, result in enumerate(results, start=1)]
