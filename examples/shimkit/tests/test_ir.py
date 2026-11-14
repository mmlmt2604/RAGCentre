from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from shimkit.ir import build_ir

DOCLING_GOLDEN = Path(__file__).parents[2] / "docling_shim" / "tests" / "golden" / "sample_ir.json"


def test_ids_and_blank_blocks() -> None:
    blocks = [
        {"type": "paragraph", "text": "one"},
        {"type": "paragraph", "text": "   "},
        {"type": "table", "text": ""},
        {"type": "figure", "text": ""},
        {"type": "paragraph", "text": "two", "id": "stale"},
    ]
    ir = build_ir(blocks, [])
    assert ir["schema_version"] == "1.0"
    assert [(b["id"], b["type"]) for b in ir["blocks"]] == [("b1", "paragraph"), ("b2", "figure"), ("b3", "paragraph")]


def test_metadata() -> None:
    blocks = [
        {"type": "heading", "level": 2, "text": "Not the title"},
        {"type": "heading", "level": 1, "text": "Pump Manual"},
        {"type": "heading", "level": 1, "text": "Second"},
    ]
    pages = [{"page": 1, "width": 10.0, "height": 20.0}, {"page": 2, "width": 10.0, "height": 20.0}]
    ir = build_ir(blocks, pages, source_name="manual.pdf", metadata={"language": "en", "title": "old"})
    assert ir["metadata"] == {"language": "en", "title": "Pump Manual", "page_count": 2, "source_name": "manual.pdf"}
    assert ir["pages"] == pages


def test_metadata_without_pages_title_or_source() -> None:
    ir = build_ir([{"type": "paragraph", "text": "x"}], [])
    assert ir["metadata"] == {} and ir["pages"] == []
    assert build_ir([], [])["blocks"] == []


def test_title_ignores_dropped_and_non_level_one_headings() -> None:
    ir = build_ir([{"type": "heading", "level": 1, "text": " "}, {"type": "heading", "level": 2, "text": "x"}], [])
    assert "title" not in ir["metadata"]


def test_inputs_are_not_modified() -> None:
    blocks = [{"type": "paragraph", "text": "x", "metadata": {"k": 1}}]
    pages = [{"page": 1, "width": 1.0, "height": 1.0}]
    meta = {"language": "en"}
    before = copy.deepcopy((blocks, pages, meta))
    ir = build_ir(blocks, pages, source_name="s", metadata=meta)
    assert (blocks, pages, meta) == before
    ir["pages"][0]["width"] = 99.0
    assert pages[0]["width"] == 1.0


def test_accepts_any_iterable_of_blocks() -> None:
    ir = build_ir(iter([{"type": "paragraph", "text": "x"}]), [])
    assert [b["id"] for b in ir["blocks"]] == ["b1"]


@pytest.mark.skipif(not DOCLING_GOLDEN.exists(), reason="docling_shim golden file not present")
def test_matches_docling_shim_for_the_shared_fields() -> None:
    """Feeding the Docling shim's own golden blocks back through ``build_ir`` (plus a blank
    block that must be dropped) reproduces that golden file exactly."""
    golden = json.loads(DOCLING_GOLDEN.read_text(encoding="utf-8"))
    blocks = [{k: v for k, v in b.items() if k != "id"} for b in golden["blocks"]]
    blocks.insert(2, {"type": "paragraph", "text": "   "})
    ir = build_ir(blocks, golden["pages"], source_name=golden["metadata"]["source_name"])
    assert ir == golden
