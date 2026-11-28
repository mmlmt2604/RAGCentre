"""Tier A self-tests of ``probes/doctr.py``: the checks it defines and the shape of its model sweep. No engine needed."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from client import Reply
from probe import BlockTruth, FixtureTruth, LineTruth, Observation, PageTruth, unmet_requirements

HERE = Path(__file__).resolve().parent
ENGINE_PY = HERE.parents[1] / "examples" / "doctr_shim" / "doctr_shim" / "engine.py"

spec = importlib.util.spec_from_file_location("ocr_live_probes_doctr_under_test", HERE / "probes" / "doctr.py")
assert spec is not None and spec.loader is not None
doctr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctr)


def truth(
    *, lines: int = 3, blocks: tuple[BlockTruth, ...] = (), size: tuple[float, float] = (1400.0, 640.0)
) -> FixtureTruth:
    page = PageTruth(
        page=1,
        width=size[0],
        height=size[1],
        tokens=("alpha",),
        lines=tuple(LineTruth(f"line {n}", ("line",), (0, n * 10, 100, n * 10 + 9)) for n in range(lines)),
        blocks=blocks,
    )
    return FixtureTruth("t.png", "image", "px", "en", "", (page,), {})


def result(
    blocks: list[dict[str, Any]], *widths: float, count: int | None = None, ratio: float = 1.3
) -> dict[str, Any]:
    pages = [{"page": n, "width": w, "height": w * ratio} for n, w in enumerate(widths or (1400.0,), start=1)]
    return {"schema_version": "1.0", "metadata": {"page_count": count or len(pages)}, "pages": pages, "blocks": blocks}


def watch(body: Any, control: Any = None, fixture: FixtureTruth | None = None) -> Observation:
    return Observation(
        run=Reply(200, body, 0.1),
        control=None if control is None else Reply(200, control, 0.1),
        truth=fixture or truth(),
        info={},
    )


def block(text: str = "x", kind: str = "paragraph", **kw: Any) -> dict[str, Any]:
    return {"type": kind, "text": text, "page": 1, **kw}


def test_page_width_ratio_accepts_the_dpi_ratio_and_refuses_another():
    check = doctr.page_width_ratio(300 / 72)
    assert check(watch(result([], 2550.0), result([], 612.0))).ok
    assert check(watch(result([], 2550.0), result([], 640.0))).ok  # 3.98, inside 5%
    assert not check(watch(result([], 1224.0), result([], 612.0))).ok  # 2x: dpi 144 against 72
    assert not check(watch(result([], 612.0), result([], 612.0))).ok  # the null shim: dpi changes nothing


def test_page_size_is_the_image_refuses_a_resampled_page():
    check = doctr.page_size_is_the_image()
    assert check(watch(result([], 1400.0, ratio=640 / 1400))).ok
    assert not check(watch(result([], 5833.0, ratio=640 / 1400))).ok  # rendered at 300 dpi as if it were a PDF


def test_one_block_per_truth_line_needs_the_lines_and_more_blocks_than_the_control():
    check = doctr.one_block_per_truth_line()
    lines, one = result([block()] * 3), result([block()])
    assert check(watch(lines, one)).ok
    assert not check(watch(one, one)).ok  # option ignored
    assert not check(watch(lines, lines)).ok  # three blocks both times
    assert not check(watch(result([block()] * 4), one)).ok  # a line split in two is not a line


def test_title_is_heading_needs_the_title_as_a_level_one_heading_and_none_without_the_option():
    title = BlockTruth("heading", "Harbour Warehouse Audit", (0, 0, 1, 1), level=1)
    fixture = truth(blocks=(title,))
    check = doctr.title_is_heading(1)
    plain = result([block("Harbour Warehouse Audit")])
    found = result([block("Harbour Warehouse Audit", "heading", level=1), block("Body text.")])
    assert check(watch(found, plain, fixture)).ok
    assert not check(watch(plain, plain, fixture)).ok  # the title is a paragraph
    assert not check(watch(found, found, fixture)).ok  # headings were on for the control too
    assert not check(watch(result([block("Harbour Warehouse Audit", "heading", level=2)]), plain, fixture)).ok
    assert not check(watch(result([block("Something else", "heading", level=1)]), plain, fixture)).ok


def test_page_count_is_needs_the_metadata_and_the_declared_pages_to_agree():
    check = doctr.page_count_is(2)
    assert check(watch(result([], 100.0, 100.0))).ok
    assert not check(watch(result([], 100.0, 100.0, 100.0))).ok
    assert not check(watch(result([], 100.0, 100.0, count=5))).ok  # page_count says the whole document


def shim_choices(name: str) -> list[str]:
    for node in ast.parse(ENGINE_PY.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return list(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in {ENGINE_PY}")


@pytest.mark.parametrize("kind, option", [("DETECTION", "detection"), ("RECOGNITION", "recognition")])
def test_every_architecture_the_shim_offers_has_a_probe(kind: str, option: str):
    probed = [p.options[option] for p in doctr.PROBES if p.id.startswith(f"doctr.{option}.")]
    assert probed == shim_choices(kind)


def test_architectures_that_are_not_downloaded_are_skipped_and_never_passed(monkeypatch):
    monkeypatch.delenv("OCR_LIVE_DOCTR_ALL_MODELS", raising=False)
    skipped = {
        p.options.get("detection") or p.options["recognition"]
        for p in doctr.PROBES
        if p.id.startswith(("doctr.detection.", "doctr.recognition.")) and unmet_requirements(p.requires, {})
    }
    offered = set(shim_choices("DETECTION")) | set(shim_choices("RECOGNITION"))
    assert skipped == offered - doctr.DEFAULT_MODELS["detection"] - doctr.DEFAULT_MODELS["recognition"]
    assert len(skipped) == 14
    monkeypatch.setenv("OCR_LIVE_DOCTR_ALL_MODELS", "1")
    assert not [p.id for p in doctr.PROBES if unmet_requirements(p.requires, {})]
