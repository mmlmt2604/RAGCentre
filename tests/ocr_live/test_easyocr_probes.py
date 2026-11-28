"""Tier A tests of the checks that only ``probes/easyocr.py`` defines. No engine and no network.

Each check is driven with a result that meets the claim and one that breaks it, so a check that always passes
(or always fails) is caught. The probe tables themselves are covered by ``test_selftest.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from client import Reply
from probe import BlockTruth, FixtureTruth, LineTruth, Observation, PageTruth

PROBES_FILE = Path(__file__).resolve().parent / "probes" / "easyocr.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("easyocr_probes_under_test", PROBES_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


E = load_module()


def truth(pages: dict[int, str], *, size: tuple[float, float] = (1400.0, 640.0), extras: Any = None) -> FixtureTruth:
    return FixtureTruth(
        name="hand_built.png",
        kind="image",
        units="px",
        language="en",
        sha256="",
        pages=tuple(
            PageTruth(
                page=n,
                width=size[0],
                height=size[1],
                tokens=tuple(text.split()),
                lines=(LineTruth(text, tuple(text.split()), (0.0, 0.0, size[0], size[1])),),
                blocks=(),
            )
            for n, text in pages.items()
        ),
        extras=extras or {},
    )


def ir(blocks: list[dict[str, Any]], pages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": "1.0", "metadata": {}, "pages": pages, "blocks": blocks}


def observe(body: Any, control: Any = None, fixture: FixtureTruth | None = None) -> Observation:
    return Observation(
        run=Reply(200, body, 0.1),
        control=None if control is None else Reply(200, control, 0.1),
        truth=fixture or truth({1: "alpha bravo"}),
        info={},
    )


def sized(width: float, height: float, page: int = 1) -> dict[str, Any]:
    return {"page": page, "width": width, "height": height}


def block(text: str, page: int = 1) -> dict[str, Any]:
    return {"type": "paragraph", "text": text, "page": page}


# --- headings_only_with_option ------------------------------------------------------------------


def titled(text: str, level: int = 1, page: int = 1) -> dict[str, Any]:
    return {"type": "heading", "level": level, "text": text, "page": page}


def with_blocks(*blocks: tuple[str, str, int | None]) -> FixtureTruth:
    base = truth({1: "x"})
    page = base.pages[0]
    truth_blocks = tuple(BlockTruth(kind, text, (0.0, 0.0, 1.0, 1.0), level) for kind, text, level in blocks)
    return replace(base, pages=(replace(page, blocks=truth_blocks),))


def test_headings_only_with_option_needs_the_truth_title_as_a_heading_and_none_in_the_control():
    check = E.headings_only_with_option(1)
    fixture = with_blocks(
        ("page_header", "Running header", None), ("heading", "Harbour Warehouse Audit", 1), ("heading", "Scope", 2)
    )
    pages = [sized(10, 10)]
    on = ir([block("Running header"), titled("Harbour Warehouse Audit"), block("text")], pages)
    off = ir([block("Running header"), block("Harbour Warehouse Audit text")], pages)
    verdict = check(observe(on, off, fixture))
    assert verdict.ok and verdict.numbers == {"headings_wanted": 1, "headings_found": 1, "control_headings": 0}
    assert not check(observe(off, off, fixture)).ok  # the title ran into the paragraph below: no heading
    assert not check(observe(on, on, fixture)).ok  # the control has headings too: the option shows nothing
    assert not check(observe(ir([titled("Harbour Warehouse Audit", level=2)], pages), off, fixture)).ok  # wrong level
    assert not check(observe(ir([titled("Sampling rule")], pages), off, fixture)).ok  # another text
    assert not check(observe(on, off, with_blocks(("paragraph", "no headings", None)))).ok  # nothing to find


def test_headings_only_with_option_asks_only_for_headings_on_the_pages_that_were_read():
    check = E.headings_only_with_option(1)
    base = truth({1: "x", 2: "y"})
    page_one, page_two = base.pages
    fixture = replace(
        base,
        pages=(
            replace(page_one, blocks=(BlockTruth("heading", "First title", (0.0, 0.0, 1.0, 1.0), 1),)),
            replace(page_two, blocks=(BlockTruth("heading", "Second title", (0.0, 0.0, 1.0, 1.0), 1),)),
        ),
    )
    on = ir([titled("Second title", page=2)], [sized(10, 10, 2)])  # only page 2 was read
    off = ir([block("Second title", page=2)], [sized(10, 10, 2)])
    assert check(observe(on, off, fixture)).ok
    assert not check(observe(ir([titled("First title", page=2)], [sized(10, 10, 2)]), off, fixture)).ok


# --- fewer_tokens_than_control ------------------------------------------------------------------


def test_fewer_tokens_than_control_counts_words_and_not_blocks():
    check = E.fewer_tokens_than_control()
    one_block, three_blocks = ir([block("alpha bravo charlie delta")], []), ir([block("alpha"), block("bravo")], [])
    verdict = check(observe(three_blocks, one_block))  # more blocks, fewer words: text was dropped
    assert verdict.ok and verdict.numbers == {"tokens": 2, "control_tokens": 4}
    assert not check(observe(one_block, one_block)).ok  # the option dropped nothing
    assert not check(observe(one_block, three_blocks)).ok  # more words than the control


# --- page_width_ratio ---------------------------------------------------------------------------


def test_page_width_ratio_accepts_the_ratio_and_rejects_the_same_size_or_a_far_one():
    check = E.page_width_ratio(300 / 72)
    wide, narrow = ir([], [sized(2550, 3300)]), ir([], [sized(612, 792)])
    good = check(observe(wide, narrow))
    assert good.ok and good.numbers["width_ratio"] == 4.1667
    assert not check(observe(narrow, narrow)).ok  # the option did nothing
    assert not check(observe(ir([], [sized(1224, 1584)]), narrow)).ok  # 2x is not 300/72
    assert check(observe(ir([], [sized(2600, 3300)]), narrow)).ok  # within 5%
    assert not check(observe(ir([], [sized(2800, 3300)]), narrow)).ok  # 6.7% off


def test_page_width_ratio_fails_when_a_page_declares_no_size():
    check = E.page_width_ratio(2.0)
    assert not check(observe(ir([], [{"page": 1}]), ir([], [sized(100, 100)]))).ok
    assert not check(observe(ir([], [sized(200, 100)]), ir([], []))).ok


# --- page_size_is_truth -------------------------------------------------------------------------


def test_page_size_is_truth_catches_a_resized_image_and_a_missing_page():
    check = E.page_size_is_truth()
    fixture = truth({1: "alpha"}, size=(1400.0, 640.0))
    assert check(observe(ir([], [sized(1400, 640)]), fixture=fixture)).ok
    assert not check(observe(ir([], [sized(700, 320)]), fixture=fixture)).ok
    assert not check(observe(ir([], []), fixture=fixture)).ok


# --- pages_read ---------------------------------------------------------------------------------


def test_pages_read_needs_exactly_the_pages_and_stores_how_many_were_read():
    check = E.pages_read({2, 3})
    pages = [sized(10, 10, 2), sized(10, 10, 3)]
    verdict = check(observe(ir([block("a", 2), block("b", 3)], pages)))
    assert verdict.ok and verdict.numbers["pages_read"] == 2
    assert not check(observe(ir([block("a", 2)], pages))).ok  # page 3 has no block
    assert not check(observe(ir([block("a", 1), block("b", 2), block("c", 3)], [sized(10, 10, 1), *pages]))).ok
    assert not check(observe(ir([block("a", 2), block("b", 3)], [*pages, sized(10, 10, 4)]))).ok  # extra declared page


# --- exact_words_pair ---------------------------------------------------------------------------

UMLAUTS = {"umlaut_words": ["prüfung", "maßen", "kühlraum"]}


def test_exact_words_pair_tells_an_accent_from_the_letter_without_it():
    fixture = truth({1: "die prüfung maßen kühlraum"}, extras=UMLAUTS)
    check = E.exact_words_pair("umlaut_words", treatment_min=0.8, control_max=0.3)
    right, stripped = ir([block("Die Prüfung maßen Kühlraum")], []), ir([block("Die Prufung maBen Kuhlraum")], [])
    verdict = check(observe(right, stripped, fixture))
    assert verdict.ok and verdict.numbers == {"umlaut_words_exact": 1.0, "umlaut_words_control_exact": 0.0}
    assert not check(observe(stripped, stripped, fixture)).ok  # the option on reads no better
    assert not check(observe(right, right, fixture)).ok  # the control is as good: the option shows nothing
    assert not check(observe(ir([block("unrelated words")], []), stripped, fixture)).ok


def test_exact_word_share_counts_a_repeated_word_once_per_occurrence_and_normalises_unicode():
    assert E.exact_word_share(ir([block("prüfung")], []), ["prüfung", "prüfung"]) == 0.5
    assert E.exact_word_share(ir([block("Prüfung")], []), ["prüfung"]) == 1.0  # decomposed ü still matches
    assert E.exact_word_share(ir([], []), []) == 1.0
