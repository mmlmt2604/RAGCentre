"""Probes for the Docling shim (``examples/docling_shim``, port 8701): ``--engine docling=URL``.

Docling is for layout: headings, tables, lists, formulas, figures. Its OCR is a pluggable engine. The rows follow
the Docling table of ``docs/plans/ocr-verification-plan.md``. Fixtures not named here are in ``make_fixtures.py``.

Docling's default ``ocr_engine`` is ``auto``, which selects an installed engine. The option probes pin
``easyocr`` to separate an option's effect from engine selection. Set ``OCR_LIVE_DOCLING_OCR_ENGINE`` to use
another installed engine for those probes. The default ``auto`` pair and one probe per engine also run.
The EasyOCR weights must already be cached for an offline run.

Probes that need something a machine may lack say so with ``requires``: the ``tesseract`` program, macOS for
``ocrmac``, and ``OCR_LIVE_DOCLING_RAPIDOCR`` for a venv that has ``onnxruntime`` installed.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from typing import Any

import predicates as P
from probe import Check, Observation, Probe, Verdict
from table_checks import html_rows as html_rows
from table_checks import table_cells

ENGINE = os.environ.get("OCR_LIVE_DOCLING_OCR_ENGINE", "easyocr")
NEEDS_TESSERACT = ("bin:tesseract",)
NEEDS_MACOS = ("platform:darwin",)
NEEDS_RAPIDOCR = ("env:OCR_LIVE_DOCLING_RAPIDOCR",)
NEEDS_PAGE_IMAGES = ("capability:page_images",)


def ocr(**options: Any) -> dict[str, Any]:
    """``options`` with the OCR engine this run reads text with (see the module docstring)."""
    return {"ocr_engine": ENGINE, **options}


# --- checks that only this engine's rows need -----------------------------------------------------

_LATEX_COMMAND = re.compile(r"\\[A-Za-z]+")


def _blocks(ir: Any, kind: str) -> list[dict[str, Any]]:
    return [b for b in P.blocks_of(ir) if b.get("type") == kind]


def _metadata(block: dict[str, Any]) -> dict[str, Any]:
    meta = block.get("metadata")
    return meta if isinstance(meta, dict) else {}


def has_block(kind: str) -> Check:
    """At least one block of type ``kind``."""

    def fn(obs: Observation) -> Verdict:
        n = len(_blocks(obs.run.body, kind))
        return Verdict(n > 0, f"{n} {kind} blocks", {f"{kind}_blocks": n})

    return Check(f"has_{kind}", "structure", fn)


def pages_covered(expected: set[int]) -> Check:
    """The pages the blocks cover are ``expected``, and the result declares the same pages. Docling joins a paragraph
    that runs over a page break into one block, and the shim gives it ``metadata.page_end``, so a block covers the
    pages from its ``page`` to its ``page_end``. (One block per page is not what Docling returns.)"""

    def fn(obs: Observation) -> Verdict:
        covered: set[int] = set()
        for block in P.blocks_of(obs.run.body):
            start = block.get("page")
            if isinstance(start, int):
                end = _metadata(block).get("page_end", start)
                covered.update(range(start, (end if isinstance(end, int) else start) + 1))
        declared = P.declared_pages(obs.run.body)
        ok = covered == expected and (not declared or declared == expected)
        return Verdict(
            ok,
            f"blocks cover pages {sorted(covered)}, declared {sorted(declared)}, expected {sorted(expected)}",
            {"pages_covered": sorted(covered)},
        )

    return Check(f"pages_covered_{sorted(expected)}", "structure", fn)


def page_count_is(count: int) -> Check:
    """``metadata.page_count`` and the declared pages both say ``count``."""

    def fn(obs: Observation) -> Verdict:
        body = obs.run.body
        meta = body.get("metadata") if isinstance(body, dict) else None
        stated = meta.get("page_count") if isinstance(meta, dict) else None
        declared = len(P.declared_pages(body))
        return Verdict(
            stated == count and declared == count,
            f"page_count {stated!r}, {declared} pages declared, expected {count}",
            {"page_count": stated},
        )

    return Check(f"page_count_is_{count}", "structure", fn)


def exact_word_share(ir: Any, words: list[str]) -> float:
    """Share of ``words`` that the result has as a token, letter for letter. ``recall`` forgives one wrong letter
    in a short word, so it cannot tell "Prüfung" from "Prufung". One result token counts for one word."""
    pool = Counter(P.result_tokens(ir))
    found = 0
    for word in (unicodedata.normalize("NFC", w).lower() for w in words):
        if pool[word] > 0:
            pool[word] -= 1
            found += 1
    return found / len(words) if words else 1.0


def exact_words_pair(key: str, treatment_min: float, control_max: float) -> Check:
    """The run has at least ``treatment_min`` of the words ``truth.extras[key]`` spelled exactly, and the control at
    most ``control_max``: the form of a language option, whose effect is the accents and letters of that language."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "exact_words_pair needs a control run"
        words = list(obs.truth.extras[key])
        on, off = exact_word_share(obs.run.body, words), exact_word_share(obs.control.body, words)
        return Verdict(
            on >= treatment_min and off <= control_max,
            f"{key} spelled exactly: {on:.2f} with the option (needs {treatment_min:.2f}), "
            f"{off:.2f} without (allowed {control_max:.2f})",
            {f"{key}_exact": round(on, 4), f"{key}_control_exact": round(off, 4)},
        )

    return Check(f"{key}_exact_pair", "recall", fn)


def _latex_commands(ir: Any) -> set[str]:
    return {c for b in _blocks(ir, "formula") for c in _LATEX_COMMAND.findall(str(b.get("text", "")))}


def formula_latex_only_with_option() -> Check:
    """With the option a ``formula`` block holds every LaTeX command of the fixture's formula (``\\frac``, ``\\sqrt``,
    ``\\pm``), and the control has no formula block with a command in it (Docling gives no formula block, only the
    paragraph around it, without the enrichment)."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "formula_latex_only_with_option needs a control run"
        wanted = set(_LATEX_COMMAND.findall(obs.truth.extras["formula"]["latex"]))
        got, control = _latex_commands(obs.run.body), _latex_commands(obs.control.body)
        problems = []
        if not wanted:
            problems.append("the truth formula has no LaTeX command")
        elif not wanted <= got:
            problems.append(f"formula blocks lack {sorted(wanted - got)}")
        if control:
            problems.append(f"the control has LaTeX commands {sorted(control)}")
        return Verdict(
            not problems,
            "; ".join(problems),
            {"formula_commands": len(got & wanted), "formula_commands_wanted": len(wanted)},
        )

    return Check("formula_latex_only_with_option", "structure", fn)


def code_language_only_with_option() -> Check:
    """With the option a ``code`` block carries ``metadata.language`` equal to the fixture's (case aside), and the
    control has a code block with no language."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "code_language_only_with_option needs a control run"
        wanted = str(obs.truth.extras["code"]["language"]).casefold()
        got = {str(_metadata(b).get("language", "")).casefold() for b in _blocks(obs.run.body, "code")}
        control = _blocks(obs.control.body, "code")
        control_languages = {str(_metadata(b).get("language", "")) for b in control} - {""}
        problems = []
        if wanted not in got:
            problems.append(f"code languages {sorted(got)}, expected {wanted!r}")
        if not control:
            problems.append("the control has no code block")
        if control_languages:
            problems.append(f"the control already names the language {sorted(control_languages)}")
        return Verdict(not problems, "; ".join(problems), {"code_languages": sorted(got - {""})})

    return Check("code_language_only_with_option", "structure", fn)


def classification_only_with_option() -> Check:
    """With the option a ``figure`` block has ``metadata.classification`` and the class is a chart (the fixture's
    figure is a bar chart). The control has no classification on any block."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "classification_only_with_option needs a control run"
        classes = [
            str(_metadata(b)["classification"])
            for b in _blocks(obs.run.body, "figure")
            if "classification" in _metadata(b)
        ]
        control = [b for b in P.blocks_of(obs.control.body) if "classification" in _metadata(b)]
        problems = []
        if not classes:
            problems.append("no figure has a classification")
        elif not any("chart" in c.lower() for c in classes):
            problems.append(f"classes {classes} are not a chart")
        if control:
            problems.append(f"the control has {len(control)} classified blocks")
        return Verdict(not problems, "; ".join(problems), {"classification": classes[0] if classes else None})

    return Check("classification_only_with_option", "structure", fn)


def heading_levels_only_with_option(at_least: int) -> Check:
    """The run's headings have ``at_least`` distinct levels and the control's share one level."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "heading_levels_only_with_option needs a control run"
        run, control = sorted(set(P.heading_levels(obs.run.body))), sorted(set(P.heading_levels(obs.control.body)))
        return Verdict(
            len(run) >= at_least and len(control) == 1,
            f"heading levels {run} with the option (needs {at_least} distinct), {control} without (needs one)",
            {"heading_levels": run, "control_heading_levels": control},
        )

    return Check(f"heading_levels_only_with_option_{at_least}", "structure", fn)


def figure_text_longer_than_control() -> Check:
    """The first figure's ``text`` is longer than the control's: the description follows the caption."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "figure_text_longer_than_control needs a control run"
        run, control = _blocks(obs.run.body, "figure"), _blocks(obs.control.body, "figure")
        if not run or not control:
            return Verdict(False, f"{len(run)} figures with the option, {len(control)} without", {})
        a, b = len(str(run[0].get("text", ""))), len(str(control[0].get("text", "")))
        return Verdict(
            a > b,
            f"figure text is {a} characters with the option, {b} without",
            {"figure_chars": a, "control_figure_chars": b},
        )

    return Check("figure_text_longer_than_control", "compare", fn)


# --- the probe table --------------------------------------------------------------------------------

PROBES = [
    # --- bad options: the answer comes before any text is read -----------------------------------------
    Probe(
        id="docling.ocr_engine_unknown",
        engine="docling",
        fixture="scan_clean.png",
        options={"ocr_engine": "no-such-engine"},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
        note="ocr_engine is a choice of five values.",
    ),
    Probe(
        id="docling.table_mode_unknown",
        engine="docling",
        fixture="scan_clean.png",
        options={"table_mode": "fastest"},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
        note="table_mode is accurate or fast.",
    ),
    Probe(
        id="docling.max_pages_below_minimum",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"max_pages": 0},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
        note="max_pages has a minimum of 1 in the schema.",
    ),
    Probe(
        id="docling.ocr_wrong_type",
        engine="docling",
        fixture="scan_clean.png",
        options={"ocr": "yes"},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
        note="ocr is a boolean. A string is a type error.",
    ),
    # --- the born-digital report, default options ------------------------------------------------------
    Probe(
        id="docling.report_defaults",
        engine="docling",
        fixture="report_born_digital.pdf",
        predicate=P.all_of(
            "report_defaults",
            P.distinct_heading_levels(2),
            table_cells(1.0),
            has_block("list_item"),
            P.no_confidence(),
            P.recall_at_least(0.9),
        ),
        tier="B",
        note="Headings at 2 or more levels, one table block (Markdown in text, html, all 12 cells in the right "
        "place), list items, no metadata.confidence (Docling reads a text layer, so it has none), and at least "
        "90% of the page text. Page furniture is not in the result: see report_page_furniture_blocks.",
    ),
    Probe(
        id="docling.heading_hierarchy_off",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"heading_hierarchy": False},
        predicate=P.headings_share_one_level(),
        tier="B",
        note="Without the hierarchy Docling puts every section header at one level (2).",
    ),
    Probe(
        id="docling.heading_hierarchy_on_by_default",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"heading_hierarchy": True},
        control={"heading_hierarchy": False},
        predicate=heading_levels_only_with_option(3),
        tier="C",
        note="With the hierarchy the report's sections, subsections and subsubsections are 3 levels (2, 3, 4). The "
        "control has one.",
    ),
    Probe(
        id="docling.report_title_is_level_1",
        engine="docling",
        fixture="report_born_digital.pdf",
        predicate=P.first_block_heading(1),
        gate="record",
        tier="C",
        note="Recorded: Docling labels the document title a section_header, not a title, so it is a level 2 heading, "
        "the same as '1 Introduction'. The IR maps only Docling's own title label to level 1.",
    ),
    Probe(
        id="docling.report_page_furniture_blocks",
        engine="docling",
        fixture="report_born_digital.pdf",
        predicate=P.all_of("page_header_and_footer_blocks", has_block("page_header"), has_block("page_footer")),
        gate="record",
        tier="C",
        note="Recorded: the running header and footer are not in the result. Docling puts them in its furniture "
        "layer and the shim reads the body layer only. That keeps repeated lines out of chunks.",
    ),
    # --- OCR on and off ----------------------------------------------------------------------------------
    Probe(
        id="docling.ocr_off_vs_on",
        engine="docling",
        fixture="scan_multipage.pdf",
        options=ocr(ocr=True),
        control=ocr(ocr=False),
        predicate=P.all_of("ocr_pair_and_pages", P.recall_pair(0.8, 0.1), pages_covered({1, 2, 3, 4, 5})),
        tier="B",
        note="With ocr the 5 image-only pages are read and with ocr false at most 10% of the words come back. "
        "Docling joins a paragraph over a page break, so 5 pages are 2 blocks and the last page is metadata.page_end.",
    ),
    Probe(
        id="docling.ocr_off_vs_on_auto_engine",
        engine="docling",
        fixture="scan_multipage.pdf",
        options={"ocr": True},
        control={"ocr": False},
        predicate=P.recall_pair(0.8, 0.1),
        gate="record",
        tier="C",
        note="The paired gate repeated with the default engine (auto). After the shim keeps picture children, the default "
        "engine also reads all five scanned pages. The earlier empty result came from dropped child text.",
    ),
    Probe(
        id="docling.force_full_page_ocr_reads_the_picture",
        engine="docling",
        fixture="mixed_embedded_image.pdf",
        options=ocr(force_full_page_ocr=True),
        predicate=P.all_of(
            "picture_text_and_page", P.extra_recall_at_least("image_tokens", 0.8), P.recall_at_least(0.9)
        ),
        tier="C",
        note="The text that is only pixels in the picture is read, and so is the page text (the text layer is OCR'd "
        "too, so 'A notice' loses its 'A': recall 0.95).",
    ),
    Probe(
        id="docling.picture_text_is_read_by_default",
        engine="docling",
        fixture="mixed_embedded_image.pdf",
        options=ocr(),
        predicate=P.extra_recall_at_least("image_tokens", 0.8),
        tier="C",
        note="Docling OCRs a picture of text on a born-digital page without force_full_page_ocr, and the text is in "
        "the result. In Docling 2.150.1 the default OCR mode uses layout regions without PDF text. The plan's "
        "assumption about a 5% bitmap threshold does not apply to this mode.",
    ),
    Probe(
        id="docling.force_full_page_ocr_plan_pair",
        engine="docling",
        fixture="mixed_embedded_image.pdf",
        options=ocr(force_full_page_ocr=True),
        control=ocr(force_full_page_ocr=False),
        predicate=P.extra_recall_pair("image_tokens", 0.8, 0.2),
        gate="record",
        tier="C",
        note="The plan's claim: the picture's words appear with force_full_page_ocr and are absent without. Recorded: "
        "they appear without it too (picture_text_is_read_by_default), so this fixture cannot show the option. "
        "Full-page mode also OCRs the text layer, which introduces errors on this fixture.",
    ),
    Probe(
        id="docling.figure_text_is_read",
        engine="docling",
        fixture="figure_with_text.pdf",
        options=ocr(),
        predicate=P.extra_recall_at_least("figure_tokens", 0.8),
        tier="C",
        note="The chart's title, axis labels and values are pixels inside the figure. Docling OCRs them as children of "
        "the picture, and they must come back as blocks after the figure.",
    ),
    # --- OCR engines, one by one -----------------------------------------------------------------------
    Probe(
        id="docling.ocr_engine_easyocr",
        engine="docling",
        fixture="scan_clean.png",
        options={"ocr_engine": "easyocr"},
        predicate=P.recall_at_least(0.8),
        tier="B",
        note="easyocr reads the clean scan.",
    ),
    Probe(
        id="docling.ocr_engine_auto",
        engine="docling",
        fixture="scan_clean.png",
        options={"ocr_engine": "auto"},
        predicate=P.recall_at_least(0.8),
        tier="C",
        note="auto picks an installed engine (ocrmac on macOS) and reads the clean scan.",
    ),
    Probe(
        id="docling.ocr_engine_ocrmac",
        engine="docling",
        fixture="scan_clean.png",
        options={"ocr_engine": "ocrmac"},
        predicate=P.recall_at_least(0.8),
        requires=NEEDS_MACOS,
        tier="C",
        note="macOS Vision reads the clean scan. The engine exists on macOS only.",
    ),
    Probe(
        id="docling.ocr_engine_tesseract",
        engine="docling",
        fixture="scan_clean.png",
        options={"ocr_engine": "tesseract", "ocr_lang": ["eng"]},
        predicate=P.recall_at_least(0.8),
        requires=NEEDS_TESSERACT,
        tier="C",
        note="tesseract reads the clean scan. ocr_lang is given because Docling's own default asks for four "
        "languages (see ocr_engine_tesseract_default_languages).",
    ),
    Probe(
        id="docling.ocr_engine_tesseract_default_languages",
        engine="docling",
        fixture="scan_clean.png",
        options={"ocr_engine": "tesseract"},
        predicate=P.recall_at_least(0.8),
        gate="record",
        requires=NEEDS_TESSERACT,
        tier="C",
        note="The plan's row for tesseract, with no ocr_lang. Recorded: Docling asks for fra, deu, spa and eng, and a "
        "Tesseract that has only eng refuses with a 422 that names the missing language ('spa').",
    ),
    Probe(
        id="docling.ocr_engine_rapidocr",
        engine="docling",
        fixture="scan_clean.png",
        options={"ocr_engine": "rapidocr"},
        predicate=P.recall_at_least(0.8),
        requires=NEEDS_RAPIDOCR,
        tier="C",
        note="rapidocr needs onnxruntime in the venv. Without it the shim answers 422 'onnxruntime is not installed', "
        "which is a clear error and not a crash, so the probe runs only where it is installed.",
    ),
    Probe(
        id="docling.ocr_lang_german_easyocr",
        engine="docling",
        fixture="scan_german.png",
        options={"ocr_engine": "easyocr", "ocr_lang": ["de"]},
        control={"ocr_engine": "easyocr", "ocr_lang": ["en"]},
        predicate=P.all_of(
            "german_read_and_spelled", P.recall_at_least(0.8), exact_words_pair("umlaut_words", 0.8, 0.3)
        ),
        tier="C",
        note="de reads the German scan with its umlauts and eszett spelled right, and en spells them without the "
        "accent. Recall cannot tell the two apart (difflib forgives one letter), so the fixture's umlaut words "
        "are compared letter for letter.",
    ),
    Probe(
        id="docling.ocr_lang_german_plan_recall",
        engine="docling",
        fixture="scan_german.png",
        options={"ocr_engine": "easyocr", "ocr_lang": ["de"]},
        control={"ocr_engine": "easyocr", "ocr_lang": ["en"]},
        predicate=P.recall_above_control(margin=0.05, treatment_min=0.8),
        gate="record",
        tier="C",
        note="The plan's claim that the wrong language lowers fuzzy recall. Measured recall is 0.982 with de and "
        "1.000 with en. The exact umlaut spelling pair above shows the language option works.",
    ),
    # --- tables ------------------------------------------------------------------------------------------
    Probe(
        id="docling.table_mode_accurate",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"table_mode": "accurate"},
        predicate=table_cells(1.0),
        gate="record",
        tier="C",
        note="Both table modes store their cell texts for comparison. The default accurate mode is also gated by "
        "report_defaults.",
    ),
    Probe(
        id="docling.table_mode_fast",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"table_mode": "fast"},
        predicate=table_cells(1.0),
        gate="record",
        tier="C",
        note="Recorded, as the plan says: fast mode returns a table with 12 cells, and the numbers store how many "
        "are in the right place. accurate (the default) is gated by report_defaults.",
    ),
    Probe(
        id="docling.table_in_an_image",
        engine="docling",
        fixture="scan_table.png",
        options=ocr(),
        predicate=table_cells(0.75),
        tier="C",
        note="A ruled table in a PNG comes back as a table block with Markdown and html, 75% of its cells in the "
        "right place.",
    ),
    # --- enrichments ---------------------------------------------------------------------------------
    Probe(
        id="docling.formula_enrichment",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"formula_enrichment": True},
        control={"formula_enrichment": False},
        predicate=formula_latex_only_with_option(),
        tier="C",
        note="With the option the formula is a formula block of LaTeX. Without it there is no formula block.",
    ),
    Probe(
        id="docling.code_enrichment",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"code_enrichment": True},
        control={"code_enrichment": False},
        predicate=code_language_only_with_option(),
        tier="C",
        note="With the option the code block names its language (Python). Without it the block has none.",
    ),
    Probe(
        id="docling.picture_classification",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"picture_classification": True},
        control={"picture_classification": False},
        predicate=classification_only_with_option(),
        tier="C",
        note="The bar chart's figure block gets a chart class in metadata.classification. Without the option it has none.",
    ),
    Probe(
        id="docling.picture_description",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"picture_description": True, "page_range": [4, 4]},
        control={"picture_description": False, "page_range": [4, 4]},
        predicate=figure_text_longer_than_control(),
        tier="C",
        note="The figure's text is the caption and then the description, so it is longer than the caption alone.",
    ),
    # --- page images and windows -------------------------------------------------------------------------
    Probe(
        id="docling.include_page_images",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"include_page_images": True, "page_range": [1, 2]},
        control={"include_page_images": False, "page_range": [1, 2]},
        predicate=P.page_images_match(exact=True),
        requires=NEEDS_PAGE_IMAGES,
        tier="C",
        note="pages[].image is a PNG of the page's size (612 by 792 for a PDF). The control has none.",
    ),
    Probe(
        id="docling.page_range_2_3",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"page_range": [2, 3]},
        predicate=P.all_of(
            "window_pages_and_recall",
            P.page_set_equals({2, 3}),
            page_count_is(2),
            P.recall_at_least(0.9, pages={2, 3}),
        ),
        tier="C",
        note="Only pages 2 and 3 are read, they keep their own numbers, and page_count is 2.",
    ),
    Probe(
        id="docling.max_pages_2",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"max_pages": 2},
        predicate=P.all_of(
            "first_two_pages", P.page_set_equals({1, 2}), page_count_is(2), P.recall_at_least(0.9, pages={1, 2})
        ),
        tier="C",
        note="max_pages 2 reads the first two pages.",
    ),
    Probe(
        id="docling.page_range_with_max_pages",
        engine="docling",
        fixture="report_born_digital.pdf",
        options={"page_range": [2, 5], "max_pages": 2},
        predicate=P.all_of(
            "window_cut_by_max_pages", P.page_set_equals({2, 3}), page_count_is(2), P.recall_at_least(0.9, pages={2, 3})
        ),
        tier="C",
        note="max_pages counts from the first page of page_range, so [2, 5] with 2 reads pages 2 and 3.",
    ),
    # --- image input: the options must reach images ----------------------------------------------------------
    Probe(
        id="docling.image_ocr_on_vs_off",
        engine="docling",
        fixture="scan_clean.png",
        options=ocr(ocr=True),
        control=ocr(ocr=False),
        predicate=P.recall_pair(0.8, 0.1),
        tier="B",
        note="The same pair as for a PDF, on an image: ocr reads the scan and ocr false does not. The plan's row, "
        "'ocr false and text appears means the options do not reach images', passes by accident on a shim that "
        "reads no image text at all, so the run with ocr has to read it.",
    ),
    Probe(
        id="docling.image_ocr_false_plan_literal",
        engine="docling",
        fixture="scan_clean.png",
        options=ocr(ocr=False),
        predicate=P.recall_at_most(0.1),
        gate="record",
        tier="C",
        note="The plan's row as written: with ocr false no text appears on scan_clean.png. It holds for a shim that "
        "reads no image at all, so it proves nothing alone. image_ocr_on_vs_off is the gate.",
    ),
    Probe(
        id="docling.image_page_images",
        engine="docling",
        fixture="scan_clean.png",
        options=ocr(include_page_images=True),
        control=ocr(include_page_images=False),
        predicate=P.page_images_match(exact=True),
        requires=NEEDS_PAGE_IMAGES,
        tier="C",
        note="An image has its own size, and its page image is the same size as the page.",
    ),
    *[
        Probe(
            id=f"docling.format_{ext}",
            engine="docling",
            fixture=f"scan_clean.{ext}",
            options=ocr(),
            predicate=P.recall_at_least(0.8),
            tier="C",
            note=f"A {ext} image is read.",
        )
        for ext in ("jpg", "webp", "bmp")
    ],
    Probe(
        id="docling.format_gif",
        engine="docling",
        fixture="scan_clean.gif",
        options=ocr(),
        predicate=P.recall_at_least(0.8),
        tier="C",
        note="Docling reads gif through Pillow and advertises image/gif in /info.mime_types, unlike the engines that "
        "refuse it with 415.",
    ),
    Probe(
        id="docling.multi_frame_tiff",
        engine="docling",
        fixture="multi_frame.tiff",
        options=ocr(),
        predicate=P.all_of("tiff_frames_and_recall", pages_covered({1, 2, 3}), P.recall_at_least(0.8)),
        tier="C",
        note="Every frame of a TIFF is a page.",
    ),
    # --- Office, HTML and text input ----------------------------------------------------------------------
    Probe(
        id="docling.sample_docx",
        engine="docling",
        fixture="sample.docx",
        predicate=P.all_of("docx_text_and_table", P.recall_at_least(0.9), table_cells(1.0), has_block("heading")),
        tier="C",
        note="A Word file parses: its words, its heading and its 3 by 2 table.",
    ),
    Probe(
        id="docling.sample_html",
        engine="docling",
        fixture="sample.html",
        predicate=P.all_of(
            "html_text_and_table",
            P.recall_at_least(0.9),
            table_cells(1.0),
            has_block("list_item"),
            P.distinct_heading_levels(2),
        ),
        tier="C",
        note="An HTML file parses: its words, 2 heading levels, its list and its 4 by 3 table.",
    ),
    Probe(
        id="docling.sample_csv",
        engine="docling",
        fixture="sample.csv",
        predicate=P.all_of("csv_table", P.recall_at_least(0.9), table_cells(1.0)),
        tier="C",
        note="A CSV file is one table block with 12 cells.",
    ),
    Probe(
        id="docling.note_md",
        engine="docling",
        fixture="note.md",
        predicate=P.all_of("markdown_text", P.recall_at_least(0.9), has_block("list_item"), has_block("heading")),
        tier="C",
        note="A Markdown file keeps its heading and list items.",
    ),
    Probe(
        id="docling.note_txt",
        engine="docling",
        fixture="note.txt",
        predicate=P.recall_at_least(0.9),
        tier="C",
        note="A plain text file parses (it is not in the shim's advertised MIME types, and Docling reads it).",
    ),
]
