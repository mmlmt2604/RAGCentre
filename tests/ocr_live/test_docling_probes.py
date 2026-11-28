"""Tier A tests of the checks that only ``probes/docling.py`` defines. No engine and no network.

Each check is driven with a result that meets the claim and one that breaks it, so a check that always passes
(or always fails) is caught. The probe tables themselves are covered by ``test_selftest.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from client import Reply
from probe import FixtureTruth, LineTruth, Observation, PageTruth

PROBES_FILE = Path(__file__).resolve().parent / "probes" / "docling.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("docling_probes_under_test", PROBES_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


D = load_module()

CELLS = [["Area", "Pallets"], ["Docks", "120"]]
FORMULA = {"formula": {"latex": "x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}"}}


def truth(pages: dict[int, str] | None = None, *, extras: Any = None) -> FixtureTruth:
    return FixtureTruth(
        name="hand_built.pdf",
        kind="pdf",
        units="pt",
        language="en",
        sha256="",
        pages=tuple(
            PageTruth(
                page=n,
                width=612.0,
                height=792.0,
                tokens=tuple(text.split()),
                lines=(LineTruth(text, tuple(text.split()), (0.0, 0.0, 612.0, 792.0)),),
                blocks=(),
            )
            for n, text in (pages or {1: "alpha bravo"}).items()
        ),
        extras=extras or {},
    )


def ir(blocks: list[dict[str, Any]], pages: list[int] | None = None, **metadata: Any) -> dict[str, Any]:
    declared = [{"page": n, "width": 612.0, "height": 792.0} for n in (pages or [])]
    return {"schema_version": "1.0", "metadata": metadata, "pages": declared, "blocks": blocks}


def observe(body: Any, control: Any = None, fixture: FixtureTruth | None = None) -> Observation:
    return Observation(
        run=Reply(200, body, 0.1),
        control=None if control is None else Reply(200, control, 0.1),
        truth=fixture or truth(),
        info={},
    )


def block(kind: str, text: str = "x", page: int = 1, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "text": text, "page": page, **fields}


# --- html_rows and table_cells ------------------------------------------------------------------

HTML = (
    "<table><caption><div>Table 1: Pallets</div></caption><tbody>"
    "<tr><th>Area</th><th>Pallets</th></tr><tr><td>Docks</td><td>1&amp;20</td></tr></tbody></table>"
)
MARKDOWN = "Table 1: Pallets\n\n| Area   |   Pallets |\n|--------|-----------|\n| Docks  |       120 |"


def table(markdown: str = MARKDOWN, html: str | None = HTML.replace("1&amp;20", "120")) -> dict[str, Any]:
    out = block("table", markdown)
    if html is not None:
        out["html"] = html
    return out


def test_html_rows_reads_header_and_data_cells_and_unescapes_text():
    assert D.html_rows(HTML) == [["Area", "Pallets"], ["Docks", "1&20"]]
    assert D.html_rows("<table><tr><td> a\n b </td></tr></table>") == [["a b"]]
    assert D.html_rows("") == []


def test_table_cells_needs_markdown_html_and_every_cell_in_its_place():
    check = D.table_cells(1.0)
    fixture = truth(extras={"table": {"cells": CELLS}})
    verdict = check(observe(ir([table()]), fixture=fixture))
    assert verdict.ok and verdict.numbers == {"tables": 1, "html_cells": 4, "cells_exact": 1.0, "cell_texts": CELLS}
    assert not check(observe(ir([block("paragraph")]), fixture=fixture)).ok  # no table block
    assert not check(observe(ir([table(html=None)]), fixture=fixture)).ok  # no html
    assert not check(observe(ir([table(markdown="Area Pallets Docks 120")]), fixture=fixture)).ok  # not Markdown


def test_table_cells_catches_shuffled_columns_that_recall_would_accept():
    shuffled = HTML.replace("<td>Docks</td><td>1&amp;20</td>", "<td>120</td><td>Docks</td>")
    check = D.table_cells(1.0)
    fixture = truth(extras={"table": {"cells": CELLS}})
    verdict = check(observe(ir([table(html=shuffled)]), fixture=fixture))
    assert not verdict.ok and verdict.numbers["cells_exact"] == 0.5  # the header row is right, the data row is not
    assert D.table_cells(0.5)(observe(ir([table(html=shuffled)]), fixture=fixture)).ok


def test_table_cells_counts_the_cells_and_picks_the_best_of_several_tables():
    check = D.table_cells(1.0)
    fixture = truth(extras={"table": {"cells": CELLS}})
    extra_row = HTML.replace("</tbody>", "<tr><td>Wing</td><td>95</td></tr></tbody>")
    assert not check(observe(ir([table(html=extra_row)]), fixture=fixture)).ok  # 6 cells, not 4
    other = table(html="<table><tr><td>zzz</td></tr></table>")
    assert check(observe(ir([other, table(html=HTML.replace("1&amp;20", "120"))]), fixture=fixture)).ok


# --- has_block ----------------------------------------------------------------------------------


def test_has_block_counts_blocks_of_one_type():
    check = D.has_block("list_item")
    assert check(observe(ir([block("list_item"), block("paragraph")]))).numbers == {"list_item_blocks": 1}
    assert not check(observe(ir([block("paragraph")]))).ok
    assert not check(observe(ir([]))).ok


# --- pages_covered and page_count_is ------------------------------------------------------------


def test_pages_covered_follows_page_end_across_a_joined_paragraph():
    check = D.pages_covered({1, 2, 3})
    joined = ir([block("paragraph", page=1, metadata={"page_end": 2}), block("paragraph", page=3)], [1, 2, 3])
    verdict = check(observe(joined))
    assert verdict.ok and verdict.numbers == {"pages_covered": [1, 2, 3]}
    assert not check(
        observe(ir([block("paragraph", page=1), block("paragraph", page=3)], [1, 2, 3]))
    ).ok  # page 2 empty
    assert not check(observe(ir([block("paragraph", page=1, metadata={"page_end": 3})], [1, 2, 3, 4]))).ok  # extra page
    assert not check(observe(ir([block("paragraph", page=1, metadata={"page_end": 4})], [1, 2, 3]))).ok  # runs past
    assert check(observe(ir([block("paragraph", page=1, metadata={"page_end": 3})], []))).ok  # no pages declared


def test_page_count_is_needs_the_metadata_and_the_declared_pages_to_agree():
    check = D.page_count_is(2)
    assert check(observe(ir([], [2, 3], page_count=2))).ok
    assert not check(observe(ir([], [2, 3], page_count=3))).ok
    assert not check(observe(ir([], [1, 2, 3], page_count=2))).ok
    assert not check(observe(ir([], [2, 3]))).ok  # no page_count


# --- exact_words_pair ---------------------------------------------------------------------------

UMLAUTS = {"umlaut_words": ["prüfung", "maßen", "kühlraum"]}


def test_exact_words_pair_tells_an_accent_from_the_letter_without_it():
    fixture = truth({1: "die prüfung maßen kühlraum"}, extras=UMLAUTS)
    check = D.exact_words_pair("umlaut_words", treatment_min=0.8, control_max=0.3)
    right = ir([block("paragraph", "Die Prüfung maßen Kühlraum")])
    stripped = ir([block("paragraph", "Die Prufung maBen Kuhlraum")])
    verdict = check(observe(right, stripped, fixture))
    assert verdict.ok and verdict.numbers == {"umlaut_words_exact": 1.0, "umlaut_words_control_exact": 0.0}
    assert not check(observe(stripped, stripped, fixture)).ok
    assert not check(observe(right, right, fixture)).ok


def test_exact_word_share_counts_a_repeated_word_once_per_occurrence_and_normalises_unicode():
    assert D.exact_word_share(ir([block("paragraph", "prüfung")]), ["prüfung", "prüfung"]) == 0.5
    assert D.exact_word_share(ir([block("paragraph", "Prüfung")]), ["prüfung"]) == 1.0  # decomposed u + diaeresis
    assert D.exact_word_share(ir([]), []) == 1.0


# --- formula, code, classification, description -------------------------------------------------


def test_formula_latex_only_with_option_needs_every_command_and_a_control_without_a_formula():
    check = D.formula_latex_only_with_option()
    fixture = truth(extras=FORMULA)
    latex = block("formula", "x = \\frac { - b \\pm \\sqrt { b } } { 2 a }")
    plain = ir([block("paragraph", "The crew estimated the price.")])
    verdict = check(observe(ir([latex]), plain, fixture))
    assert verdict.ok and verdict.numbers == {"formula_commands": 3, "formula_commands_wanted": 3}
    assert not check(observe(plain, plain, fixture)).ok  # the option did nothing
    assert not check(observe(ir([latex]), ir([latex]), fixture)).ok  # the control has the formula too
    assert not check(observe(ir([block("formula", "x = 2")]), plain, fixture)).ok  # a formula without LaTeX
    assert not check(observe(ir([block("formula", "\\frac { a } { b }")]), plain, fixture)).ok  # lacks \sqrt


def test_code_language_only_with_option_needs_the_language_with_it_and_a_code_block_without():
    check = D.code_language_only_with_option()
    fixture = truth(extras={"code": {"language": "python"}})
    named = ir([block("code", "def f(): pass", metadata={"language": "Python"})])
    bare = ir([block("code", "def f(): pass")])
    assert check(observe(named, bare, fixture)).ok
    assert not check(observe(bare, bare, fixture)).ok  # no language
    assert not check(observe(named, named, fixture)).ok  # the control names it too
    assert not check(observe(named, ir([]), fixture)).ok  # the control lost the block
    assert not check(observe(ir([block("code", "x", metadata={"language": "Java"})]), bare, fixture)).ok


def test_classification_only_with_option_needs_a_chart_class_and_none_in_the_control():
    check = D.classification_only_with_option()
    classified = ir([block("figure", "Figure 1", metadata={"classification": "bar_chart"})])
    plain = ir([block("figure", "Figure 1")])
    verdict = check(observe(classified, plain))
    assert verdict.ok and verdict.numbers == {"classification": "bar_chart"}
    assert not check(observe(plain, plain)).ok
    assert not check(observe(classified, classified)).ok
    assert not check(observe(ir([block("figure", "F", metadata={"classification": "logo"})]), plain)).ok


def test_figure_text_longer_than_control_compares_the_first_figure_of_each():
    check = D.figure_text_longer_than_control()
    described = ir([block("figure", "Figure 1\n\nA bar chart of pallets.")])
    plain = ir([block("figure", "Figure 1")])
    assert check(observe(described, plain)).ok
    assert not check(observe(plain, plain)).ok
    assert not check(observe(plain, described)).ok
    assert not check(observe(ir([]), plain)).ok  # no figure with the option


def test_heading_levels_only_with_option_needs_levels_with_it_and_one_level_without():
    check = D.heading_levels_only_with_option(3)

    def headings(*levels: int) -> dict[str, Any]:
        return ir([block("heading", "h", level=level) for level in levels])

    assert check(observe(headings(2, 3, 4), headings(2, 2, 2))).ok
    assert not check(observe(headings(2, 2), headings(2, 2))).ok  # the option added no levels
    assert not check(observe(headings(2, 3, 4), headings(2, 3, 4))).ok  # the control has levels too
    assert not check(observe(headings(2, 3, 4), headings())).ok  # the control has no headings at all
