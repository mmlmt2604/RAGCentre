"""Paragraph grouping, row merging and heading guesses for line-only engines (shimkit.layout)."""

from __future__ import annotations

from typing import Any

import pytest

from shimkit import Line, Paragraph, group_paragraphs, guess_heading_levels, join_lines, lines_to_ir, merge_rows


def line(text: str, x0: float, y0: float, x1: float, y1: float, confidence: float | None = None) -> Line:
    return Line(text, (x0, y0, x1, y1), confidence)


def test_merge_rows_joins_pieces_of_one_line_left_to_right() -> None:
    pieces = [
        line("surface.", 330, 101, 420, 124, 0.8),
        line("Mount the pump", 100, 100, 260, 124, 1.0),
        line("on a level", 270, 100, 325, 124, 0.9),
    ]
    (merged,) = merge_rows(pieces)
    assert merged.text == "Mount the pump on a level surface."
    assert merged.bbox == (100, 100, 420, 124)
    assert merged.confidence == pytest.approx(0.9)


def test_merge_rows_keeps_far_apart_pieces_and_other_rows_separate() -> None:
    pieces = [
        line("Flow", 100, 100, 160, 124),
        line("40 l/min", 600, 100, 700, 124),  # a table column: far to the right
        line("next row", 100, 140, 200, 164),
    ]
    assert [m.text for m in merge_rows(pieces)] == ["Flow", "40 l/min", "next row"]


def test_merge_rows_without_confidences() -> None:
    (merged,) = merge_rows([line("a", 0, 0, 10, 10), line("b", 12, 0, 20, 10)])
    assert merged.confidence is None


def test_group_paragraphs_by_spacing_overlap_and_height() -> None:
    lines = [
        line("Pump Manual", 100, 80, 500, 136),  # tall: a title, not the text under it
        line("Mount the pump on a level", 100, 150, 900, 174),
        line("surface and fix it.", 100, 180, 600, 204),
        line("Close the valve.", 100, 260, 500, 284),  # a gap of 56 px: a new paragraph
        line("Right column", 1000, 180, 1300, 204),  # side by side: separate
    ]
    paragraphs = group_paragraphs(lines)
    assert [p.text for p in paragraphs] == [
        "Pump Manual",
        "Mount the pump on a level surface and fix it.",
        "Right column",
        "Close the valve.",
    ]
    assert paragraphs[1].bbox == (100, 150, 900, 204)


def test_join_lines_rejoins_hyphenated_words_only() -> None:
    assert join_lines(["an exam-", "ple of", "", "well-", "Known"]) == "an example of well- Known"
    assert join_lines(["pages 10-", "12"]) == "pages 10- 12"


def test_paragraph_confidence_and_height() -> None:
    p = Paragraph([line("a", 0, 0, 1, 10, 0.5), line("b", 0, 12, 1, 24, None), line("c", 0, 26, 1, 40, 1.0)])
    assert p.confidence == 0.75 and p.line_height == 12


def para(text: str, height: float, lines: int = 1) -> Paragraph:
    return Paragraph([line(text, 0, i * height * 2, 100, i * height * 2 + height) for i in range(lines)])


def test_guess_heading_levels_by_size() -> None:
    paragraphs = [
        para("Pump Manual", 56),
        para("Installation", 40),
        para("Mount the pump.", 24, 20),  # the body text sets the median height
        para("Specifications", 32),
        para("Flow", 24, 2),
        para("Ends with a stop.", 40),  # a sentence, not a heading
        para("A long line " * 6, 40),  # 18 words: too many
        para("1234", 40),  # no letters
        para("Three line title", 40, 3),  # too many lines
    ]
    assert guess_heading_levels(paragraphs) == [1, 2, None, 3, None, None, None, None, None]


def test_guess_heading_levels_similar_sizes_share_a_level_and_levels_are_capped() -> None:
    sizes = [100, 95, 70, 55, 45, 36]
    paragraphs = [para(f"H{i}", s) for i, s in enumerate(sizes)] + [para("body", 20, 8)]
    assert guess_heading_levels(paragraphs, max_levels=4) == [1, 1, 2, 3, 4, 4, None]
    assert guess_heading_levels([]) == []


def sample_pages() -> list[dict[str, Any]]:
    return [
        {
            "page": 2,
            "width": 1000,
            "height": 1400,
            "lines": [{"text": "Specifications", "bbox": [100, 80, 400, 124], "confidence": 0.9}],
        },
        {
            "page": 1,
            "width": 1000,
            "height": 1400,
            "image": {"mime": "image/png", "data_b64": "AAAA"},
            "lines": [
                {"text": "Pump Manual", "bbox": [100, 80, 500, 136], "confidence": 0.99},
                {"text": "Mount the pump.", "bbox": [100, 200, 500, 224], "confidence": 0.95},
                {"text": "  ", "bbox": [0, 0, 1, 1], "confidence": 1.0},  # blank: dropped
                {"text": "noise", "bbox": [0, 300, 50, 324], "confidence": 0.1},
                {"text": "Close the valve.", "bbox": [100, 400, 500, 424], "confidence": None},
            ],
        },
    ]


def test_lines_to_ir() -> None:
    ir = lines_to_ir(sample_pages(), source_name="m.pdf", headings=True, min_confidence=0.5)
    assert ir["metadata"] == {"page_count": 2, "title": "Pump Manual", "source_name": "m.pdf"}
    assert ir["pages"] == [
        {"page": 1, "width": 1000.0, "height": 1400.0},
        {"page": 2, "width": 1000.0, "height": 1400.0},
    ]
    assert [(b["type"], b.get("level"), b["text"], b["page"]) for b in ir["blocks"]] == [
        ("heading", 1, "Pump Manual", 1),
        ("paragraph", None, "Mount the pump.", 1),
        ("paragraph", None, "Close the valve.", 1),
        ("heading", 2, "Specifications", 2),
    ]
    assert ir["blocks"][0]["bbox"] == [100.0, 80.0, 500.0, 136.0]
    assert ir["blocks"][0]["metadata"] == {"confidence": 0.99}
    assert "metadata" not in ir["blocks"][2]  # no confidence known


def test_lines_to_ir_defaults_and_page_images() -> None:
    ir = lines_to_ir(sample_pages(), include_page_images=True)
    assert {b["type"] for b in ir["blocks"]} == {"paragraph"}  # headings are opt-in
    assert "noise" in [b["text"] for b in ir["blocks"]]  # no confidence floor by default
    assert ir["pages"][0]["image"] == {"mime": "image/png", "data_b64": "AAAA"}
    assert "image" not in ir["pages"][1]


def test_lines_to_ir_one_block_per_line() -> None:
    pages = [
        {
            "page": 1,
            "width": 100,
            "height": 100,
            "lines": [
                {"text": "one", "bbox": [0, 0, 50, 10]},
                {"text": "two", "bbox": [0, 12, 50, 22]},
            ],
        }
    ]
    assert [b["text"] for b in lines_to_ir(pages)["blocks"]] == ["one two"]
    assert [b["text"] for b in lines_to_ir(pages, paragraphs=False)["blocks"]] == ["one", "two"]


def one_page(*rows: tuple[str, float, float]) -> list[dict[str, Any]]:
    """A page of full-width lines ``(text, top, height)``, 600 wide, all starting at x 100."""
    lines = [{"text": text, "bbox": [100, top, 700, top + height], "confidence": 0.95} for text, top, height in rows]
    return [{"page": 1, "width": 800, "height": 1200, "lines": lines}]


def kinds(ir: dict[str, Any]) -> list[tuple[str, int | None, str]]:
    return [(b["type"], b.get("level"), b["text"]) for b in ir["blocks"]]


BODY = "Goods arrive by truck in the early morning and leave by sea"


def test_a_heading_a_little_taller_than_the_text_next_to_it_is_not_absorbed_into_a_paragraph() -> None:
    # 56 px against 40 px is 1.4 times as tall: a heading by the 1.25 rule, but within the 30% height tolerance that
    # lets lines of one paragraph differ, and 14 px apart. It must still be a block of its own, above and below.
    pages = one_page(
        ("Harbour Warehouse Audit", 100, 56),
        (BODY, 170, 40),
        (BODY, 215, 40),
        (BODY, 260, 40),
        ("Results", 310, 56),
        (BODY, 380, 40),
        (BODY, 425, 40),
    )
    assert kinds(lines_to_ir(pages, headings=True)) == [
        ("heading", 1, "Harbour Warehouse Audit"),
        ("paragraph", None, " ".join([BODY] * 3)),
        ("heading", 1, "Results"),
        ("paragraph", None, " ".join([BODY] * 2)),
    ]
    assert {b["type"] for b in lines_to_ir(pages)["blocks"]} == {"paragraph"}  # headings stay opt-in


def test_a_heading_of_two_lines_stays_one_block() -> None:
    pages = one_page(
        ("Harbour Warehouse", 100, 56), ("Audit Report", 162, 56), (BODY, 240, 40), (BODY, 285, 40), (BODY, 330, 40)
    )
    assert kinds(lines_to_ir(pages, headings=True))[0] == ("heading", 1, "Harbour Warehouse Audit Report")


def test_a_tall_line_of_body_text_does_not_split_its_paragraph() -> None:
    # The third line is 1.3 times as tall (a tall box around a descender), but it is a sentence, not a heading.
    pages = one_page((BODY, 100, 40), (BODY, 145, 40), (BODY + " and it was counted twice.", 190, 52))
    assert [k[0] for k in kinds(lines_to_ir(pages, headings=True))] == ["paragraph"]
