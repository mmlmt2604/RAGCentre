"""The MinerU, PaddleOCR, Surya, EasyOCR and docTR shims' output is valid Document IR.

The shims (examples/mineru_shim, paddleocr_shim, surya_shim, easyocr_shim, doctr_shim) cannot share an environment with
ragcentre (each engine pins its own dependencies), so their tests write the IR for a sample
document, the two-page "Pump Manual", to a golden file, and this test validates those files with
RAGCentre's own IR validation and chunking. tests/test_m10_docling_shim_golden.py does the same for
the Docling shim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ragcentre.chunking.structure import StructureStrategy
from ragcentre.models.ir import normalise_ir

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BASIC = {"heading", "paragraph", "table", "figure"}
# PaddleOCR has no list label (a bullet comes out as a paragraph), so its sample has no list_item.
GOLDENS = [
    pytest.param("mineru_shim", BASIC | {"list_item"}, id="mineru"),
    pytest.param("paddleocr_shim", BASIC, id="paddleocr"),
    pytest.param("surya_shim", BASIC | {"list_item"}, id="surya"),
    # EasyOCR and docTR read lines only: paragraphs, and headings guessed from text height.
    pytest.param("easyocr_shim", {"heading", "paragraph"}, id="easyocr"),
    pytest.param("doctr_shim", {"heading", "paragraph"}, id="doctr"),
]


@pytest.mark.parametrize(("shim", "required_types"), GOLDENS)
def test_shim_golden_ir_is_valid(shim: str, required_types: set[str]) -> None:
    golden = EXAMPLES / shim / "tests" / "golden" / "sample_ir.json"
    doc = normalise_ir(json.loads(golden.read_text(encoding="utf-8")))
    paths = {b.text: b.heading_path for b in doc.blocks}
    assert paths["Mount the pump on a level surface."] == ["Pump Manual", "Installation"]
    assert paths["Specifications"] == ["Pump Manual", "Installation", "Specifications"]
    assert {b.type for b in doc.blocks} >= required_types
    assert all(b.page in (1, 2) and b.bbox is not None for b in doc.blocks)
    options = StructureStrategy.options_model()
    chunks = StructureStrategy().chunk(doc, options)
    assert any("40 l/min" in c.text for c in chunks)
