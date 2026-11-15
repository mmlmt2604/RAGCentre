from __future__ import annotations

import pytest

from ragcentre.models.ir import IRError, normalise_ir


def ir(*blocks: dict[str, object], version: str = "1.0") -> dict[str, object]:
    return {"schema_version": version, "blocks": list(blocks)}


def test_unknown_block_type_becomes_paragraph() -> None:
    doc = normalise_ir(ir({"type": "sidebar", "text": "x", "metadata": {"k": 1}}))
    b = doc.blocks[0]
    assert b.type == "paragraph"
    assert b.metadata == {"k": 1, "original_type": "sidebar"}


def test_missing_ids_are_assigned() -> None:
    doc = normalise_ir(
        ir(
            {"type": "paragraph", "text": "a"},
            {"id": "x", "type": "paragraph", "text": "b"},
            {"type": "paragraph", "text": "c"},
        )
    )
    assert [b.id for b in doc.blocks] == ["b1", "x", "b3"]


def test_assigned_id_does_not_collide_with_explicit() -> None:
    doc = normalise_ir(ir({"type": "paragraph", "text": "a"}, {"id": "b1", "type": "paragraph", "text": "b"}))
    ids = [b.id for b in doc.blocks]
    assert len(set(ids)) == 2 and ids[1] == "b1"


def test_duplicate_ids_rejected() -> None:
    with pytest.raises(IRError, match="duplicate"):
        normalise_ir(ir({"id": "a", "type": "paragraph", "text": "x"}, {"id": "a", "type": "paragraph", "text": "y"}))


@pytest.mark.parametrize("version", ["2.0", "0.9", "x"])
def test_wrong_major_version_rejected(version: str) -> None:
    with pytest.raises(IRError, match="schema_version"):
        normalise_ir(ir({"type": "paragraph", "text": "x"}, version=version))


def test_any_1x_accepted() -> None:
    assert normalise_ir(ir(version="1.7")).blocks == []


def test_missing_version_and_not_object() -> None:
    with pytest.raises(IRError):
        normalise_ir({"blocks": []})
    with pytest.raises(IRError):
        normalise_ir([1, 2])


def test_required_fields() -> None:
    with pytest.raises(IRError):
        normalise_ir(ir({"type": "paragraph"}))
    with pytest.raises(IRError, match="level"):
        normalise_ir(ir({"type": "heading", "text": "No level"}))
    with pytest.raises(IRError, match="level"):
        normalise_ir(ir({"type": "heading", "text": "Bad", "level": 7}))
    with pytest.raises(IRError):
        normalise_ir(ir({"type": "paragraph", "text": "x", "bbox": [1, 2, 3]}))
    with pytest.raises(IRError):
        normalise_ir(ir({"type": "paragraph", "text": "x", "page": 0}))


def test_heading_paths() -> None:
    doc = normalise_ir(
        ir(
            {"type": "paragraph", "text": "preamble"},
            {"type": "heading", "level": 1, "text": "Guide"},
            {"type": "paragraph", "text": "intro"},
            {"type": "heading", "level": 2, "text": "Setup"},
            {"type": "paragraph", "text": "install"},
            {"type": "heading", "level": 3, "text": "Linux"},
            {"type": "paragraph", "text": "apt"},
            {"type": "heading", "level": 2, "text": "Usage"},
            {"type": "paragraph", "text": "run"},
            {"type": "heading", "level": 1, "text": "Appendix"},
            {"type": "paragraph", "text": "end", "level": 3},
        )
    )
    paths = [b.heading_path for b in doc.blocks]
    assert paths == [
        [],
        ["Guide"],
        ["Guide"],
        ["Guide", "Setup"],
        ["Guide", "Setup"],
        ["Guide", "Setup", "Linux"],
        ["Guide", "Setup", "Linux"],
        ["Guide", "Usage"],
        ["Guide", "Usage"],
        ["Appendix"],
        ["Appendix"],
    ]
    assert doc.blocks[-1].level is None  # level ignored on non-headings


def test_heading_level_skip() -> None:
    doc = normalise_ir(
        ir(
            {"type": "heading", "level": 3, "text": "Deep"},
            {"type": "heading", "level": 1, "text": "Top"},
            {"type": "paragraph", "text": "x"},
        )
    )
    assert doc.blocks[2].heading_path == ["Top"]


def test_page_images_stripped_on_request() -> None:
    raw = ir({"type": "paragraph", "text": "x", "page": 1})
    raw["pages"] = [{"page": 1, "width": 1.0, "height": 2.0, "image": {"mime": "image/png", "data_b64": "AAA"}}]
    data = normalise_ir(raw).without_page_images()
    assert "image" not in data["pages"][0]
    assert data["blocks"][0]["heading_path"] == []


# --- schema 1.1: confidence and spans ----------------------------------------------------------------


def test_confidence_and_spans_are_kept() -> None:
    spans = [{"page": 1, "bbox": [1, 2, 3, 4]}, {"page": 2, "bbox": [5, 6, 7, 8], "note": "kept"}]
    doc = normalise_ir(
        ir(
            {"type": "paragraph", "text": "a", "page": 1, "bbox": [1, 2, 3, 4], "confidence": 0.5, "spans": spans},
            version="1.1",
        )
    )
    block = doc.blocks[0]
    assert block.confidence == 0.5 and block.spans is not None
    assert [(s.page, s.bbox) for s in block.spans] == [(1, [1, 2, 3, 4]), (2, [5, 6, 7, 8])]
    dumped = doc.without_page_images()["blocks"][0]
    assert dumped["confidence"] == 0.5 and dumped["spans"][1]["page"] == 2 and dumped["spans"][1]["note"] == "kept"


def test_a_1_0_document_has_neither_field() -> None:
    block = normalise_ir(ir({"type": "paragraph", "text": "a"})).blocks[0]
    assert block.confidence is None and block.spans is None


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 7, "high"])
def test_confidence_must_be_a_number_from_0_to_1(confidence: object) -> None:
    with pytest.raises(IRError, match="confidence"):
        normalise_ir(ir({"type": "paragraph", "text": "a", "confidence": confidence}, version="1.1"))


@pytest.mark.parametrize("confidence", [0, 1, 0.0, 1.0, 0.999])
def test_confidence_bounds_are_inclusive(confidence: float) -> None:
    assert (
        normalise_ir(ir({"type": "paragraph", "text": "a", "confidence": confidence})).blocks[0].confidence
        == confidence
    )


@pytest.mark.parametrize(
    "spans",
    [
        [{"page": 0, "bbox": [1, 2, 3, 4]}],
        [{"page": 1, "bbox": [1, 2, 3]}],
        [{"page": 1}],
        [{"bbox": [1, 2, 3, 4]}],
        "page 1",
    ],
)
def test_bad_spans_are_rejected(spans: object) -> None:
    with pytest.raises(IRError, match="spans"):
        normalise_ir(ir({"type": "paragraph", "text": "a", "spans": spans}, version="1.1"))
