"""Live checks against the real Docling.

Skipped unless ``SHIM_LIVE=1`` and Docling can be imported. Run it in an environment that has Docling
and its models (the first run downloads them)::

    SHIM_LIVE=1 uv run pytest tests/test_live.py -s
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from docling_shim.options import (
    ENGINE_FIELDS,
    NAMED_FIELDS,
    NOT_EXPOSED,
    VLM_ONLY_FIELDS,
    Options,
    denied_key,
    parse_options,
)

live = pytest.mark.skipif(
    os.environ.get("SHIM_LIVE") != "1" or importlib.util.find_spec("docling") is None,
    reason="set SHIM_LIVE=1 in an environment with Docling installed",
)


def test_every_field_that_is_not_exposed_is_refused_by_the_denylist() -> None:
    """Runs everywhere: the three literal sets must not lie about what ``engine`` accepts."""
    for name in NOT_EXPOSED:
        assert denied_key({name: 1}) == name, f"{name} is listed as not exposed but is not denied"
    assert not set(NOT_EXPOSED) & (NAMED_FIELDS | ENGINE_FIELDS)
    for name in ENGINE_FIELDS:
        assert denied_key({name: {}}) is None, f"{name} is listed as settable but is denied"


@live
def test_engine_parameters_accounted_for() -> None:
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    fields = set(PdfPipelineOptions.model_fields)
    known = NAMED_FIELDS | ENGINE_FIELDS | set(NOT_EXPOSED)
    assert fields - known == set(), f"new Docling fields: decide where they belong: {sorted(fields - known)}"
    assert known - fields == set(), f"fields Docling no longer has: {sorted(known - fields)}"


@live
def test_vlm_pipeline_fields_accounted_for() -> None:
    from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions

    fields = set(VlmPipelineOptions.model_fields)
    known = NAMED_FIELDS | ENGINE_FIELDS | set(NOT_EXPOSED) | VLM_ONLY_FIELDS
    assert fields - known == set(), f"new VlmPipelineOptions fields: decide where they belong: {sorted(fields - known)}"
    assert fields >= VLM_ONLY_FIELDS
    presets = set(VlmConvertOptions.list_preset_ids())
    assert {"granite_docling", "smoldocling"} <= presets, "an offered vlm_model preset no longer exists"


@live
def test_the_default_options_build_doclings_default_pipeline() -> None:
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    from docling_shim.engine import build_pipeline_options

    assert build_pipeline_options(parse_options({})).model_dump() == PdfPipelineOptions().model_dump()


@live
def test_every_named_option_maps_to_a_real_field() -> None:
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    fields = set(PdfPipelineOptions.model_fields)
    for paths in __import__("docling_shim.options", fromlist=["OVERLAPS"]).OVERLAPS.values():
        for path in paths:
            assert path[0] in fields, path
    assert set(Options.model_fields) >= {"engine", "page_range"}
