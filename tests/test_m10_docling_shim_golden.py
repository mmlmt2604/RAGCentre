"""The Docling shim's output is valid Document IR.

The shim (examples/docling_shim) cannot share an environment with ragcentre (docling-core pins an
older typer), so its tests write the IR for a sample document to a golden file, and this test
validates that file with RAGCentre's own IR validation and chunking.
"""

from __future__ import annotations

import json
from pathlib import Path

from ragcentre.chunking.structure import StructureStrategy
from ragcentre.models.ir import normalise_ir

GOLDEN = Path(__file__).resolve().parent.parent / "examples" / "docling_shim" / "tests" / "golden" / "sample_ir.json"


def test_shim_golden_ir_is_valid() -> None:
    doc = normalise_ir(json.loads(GOLDEN.read_text(encoding="utf-8")))
    paths = {b.text: b.heading_path for b in doc.blocks}
    assert paths["Mount the pump on a level surface."] == ["Pump Manual", "Installation"]
    assert paths["Specifications"] == ["Pump Manual", "Installation", "Specifications"]
    assert {b.type for b in doc.blocks} >= {"heading", "paragraph", "list_item", "table", "figure"}
    assert all(b.page in (1, 2) and b.bbox is not None for b in doc.blocks)
    options = StructureStrategy.options_model()
    chunks = StructureStrategy().chunk(doc, options)
    assert any("40 l/min" in c.text for c in chunks)
