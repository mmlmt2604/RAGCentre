"""Probes for the EasyOCR shim (``examples/easyocr_shim``, port 8705): ``--engine easyocr=URL``.

EasyOCR is for many scripts and photographed text. It returns plain paragraphs with a confidence per
block. The rows follow the EasyOCR table of ``docs/plans/ocr-verification-plan.md``. Fixtures not named
here are in ``make_fixtures.py``.

Two probes need something a machine may lack and say so with ``requires``: the Chinese recognition model
(about 100 MB, downloaded on first use: set ``OCR_LIVE_ALLOW_MODEL_DOWNLOAD=1``) and the page images
capability of the shim.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from typing import Any

import predicates as P
from probe import Check, Observation, Probe, Verdict

NEEDS_DOWNLOAD = ("env:OCR_LIVE_ALLOW_MODEL_DOWNLOAD",)
FIRST_PAGE = {1}


# --- checks that only this engine's rows need -----------------------------------------------------


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


def fewer_tokens_than_control() -> Check:
    """The run's blocks hold fewer word tokens than the control's: text was dropped. Block counts do not show it,
    because dropping a low-confidence piece can split one paragraph into several."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "fewer_tokens_than_control needs a control run"
        run, control = len(P.result_tokens(obs.run.body)), len(P.result_tokens(obs.control.body))
        return Verdict(
            run < control,
            f"{run} tokens with the option, {control} without",
            {"tokens": run, "control_tokens": control},
        )

    return Check("fewer_tokens_than_control", "compare", fn)


def headings_only_with_option(level: int = 1) -> Check:
    """Every heading of ``level`` in the fixture's truth (on the pages the run declares) comes back as a heading of
    that level (its words recalled at 0.8 or more), and the control has no heading block at all."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "headings_only_with_option needs a control run"
        pages = P.declared_pages(obs.run.body)
        wanted = [
            P.tokenize(b.text)
            for page in obs.truth.pages
            if page.page in pages
            for b in page.blocks
            if b.type == "heading" and b.level == level
        ]
        got = [
            P.tokenize(str(b.get("text", "")))
            for b in P.blocks_of(obs.run.body)
            if b.get("type") == "heading" and b.get("level") == level
        ]
        found = sum(any(P.recall(tokens, heading) >= P.RATIO for heading in got) for tokens in wanted)
        control_headings = len(P.heading_levels(obs.control.body))
        problems = []
        if not wanted:
            problems.append(f"the truth has no level {level} heading on pages {sorted(pages)}")
        elif found < len(wanted):
            problems.append(
                f"{found} of {len(wanted)} level {level} headings found; run has {len(got)} such heading blocks"
            )
        if control_headings:
            problems.append(f"the control has {control_headings} heading blocks")
        return Verdict(
            not problems,
            "; ".join(problems),
            {"headings_wanted": len(wanted), "headings_found": found, "control_headings": control_headings},
        )

    return Check(f"headings_level_{level}_only_with_option", "structure", fn)


def page_width_ratio(expected: float, tolerance: float = 0.05, page: int = 1) -> Check:
    """The page ``width`` of the run is ``expected`` times that of the control, within ``tolerance``: the
    page was rendered at another resolution."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "page_width_ratio needs a control run"
        run, control = P.page_dims(obs.run.body).get(page), P.page_dims(obs.control.body).get(page)
        if run is None or control is None or control[0] <= 0:
            return Verdict(False, f"page {page} declares no size in both runs", {})
        ratio = run[0] / control[0]
        return Verdict(
            abs(ratio / expected - 1) <= tolerance,
            f"page width {run[0]:g} against {control[0]:g}: ratio {ratio:.3f}, expected {expected:.3f} +-{tolerance:.0%}",
            {"width": run[0], "control_width": control[0], "width_ratio": round(ratio, 4)},
        )

    return Check(f"page_width_ratio_{expected:.2f}", "geometry", fn)


def page_size_is_truth() -> Check:
    """Every page the result declares has the size the fixture has: the option did not resize an image."""

    def fn(obs: Observation) -> Verdict:
        dims = P.page_dims(obs.run.body)
        wrong = [
            f"page {p.page} is {dims.get(p.page)}, the fixture is {p.width:g}x{p.height:g}"
            for p in obs.truth.pages
            if dims.get(p.page) != (p.width, p.height)
        ]
        return Verdict(not wrong, "; ".join(wrong[:3]), {"pages": len(dims)})

    return Check("page_size_is_truth", "geometry", fn)


def pages_read(expected: set[int]) -> Check:
    """The pages that carry blocks are ``expected`` and the result declares the same pages. Stores the count."""

    inner = P.page_set_equals(expected)

    def fn(obs: Observation) -> Verdict:
        verdict = inner(obs)
        return Verdict(verdict.ok, verdict.detail, {**verdict.numbers, "pages_read": len(P.block_pages(obs.run.body))})

    return Check(f"pages_read_{sorted(expected)}", "structure", fn)


# --- the probe table --------------------------------------------------------------------------------

PROBES = [
    # --- bad options and unsupported input: the answer comes before any text is read -------------------
    Probe(
        id="easyocr.dpi_below_minimum",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"dpi": 10},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
        note="dpi has a minimum of 72 in the schema.",
    ),
    Probe(
        id="easyocr.decoder_unknown",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"decoder": "no-such-decoder"},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
        note="decoder is a choice of three values.",
    ),
    Probe(
        id="easyocr.canvas_size_below_minimum",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"canvas_size": 100},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
        note="canvas_size has a minimum of 256 in the schema.",
    ),
    Probe(
        id="easyocr.languages_wrong_type",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"languages": "en"},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
        note="languages is a list of codes. A bare string is a type error, not a one-language list.",
    ),
    # --- defaults ------------------------------------------------------------------------------------
    Probe(
        id="easyocr.default_recall",
        engine="easyocr",
        fixture="scan_clean.png",
        predicate=P.recall_at_least(0.85),
        tier="B",
        note="Default options read a clean English scan.",
    ),
    Probe(
        id="easyocr.blocks_have_boxes_and_confidence",
        engine="easyocr",
        fixture="scan_clean.png",
        predicate=P.all_of("boxes_and_confidence", P.boxes_inside_pages(), P.confidence_in_range(require_all=True)),
        tier="B",
        note="The README says every block has a bbox in page pixels and metadata.confidence.",
    ),
    # --- languages -------------------------------------------------------------------------------------
    Probe(
        id="easyocr.languages_de_reads_german",
        engine="easyocr",
        fixture="scan_german.png",
        options={"languages": ["de"]},
        control={"languages": ["en"]},
        predicate=P.all_of(
            "german_read_and_spelled", P.recall_at_least(0.8), exact_words_pair("umlaut_words", 0.8, 0.3)
        ),
        tier="B",
        note="German is read with de (recall of at least 80%) and its umlauts and eszett are spelled right. The "
        "plan's 'recall higher with de than with en' cannot be seen: en reads 'Prufung' and 'maBen', which the "
        "0.8 difflib tolerance still counts as found (recall 1.00 with both). Exact spelling of the fixture's "
        "umlaut words tells the two apart.",
    ),
    Probe(
        id="easyocr.languages_chinese",
        engine="easyocr",
        fixture="scan_chinese.png",
        options={"languages": ["ch_sim", "en"]},
        control={"languages": ["en"]},
        predicate=P.recall_pair(treatment_min=0.7, control_max=0.3),
        requires=NEEDS_DOWNLOAD,
        tier="C",
        note="ch_sim reads the Chinese scan and en alone cannot. The model is downloaded on first use.",
    ),
    Probe(
        id="easyocr.languages_unknown_code",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"languages": ["xx"]},
        expect_status=400,
        expect_code="invalid_options",
        tier="B",
        note="The error comes from the real library: easyocr.Reader raises ValueError for an unknown code.",
    ),
    Probe(
        id="easyocr.languages_incompatible_scripts",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"languages": ["ch_sim", "de"]},
        expect_status=400,
        expect_code="invalid_options",
        tier="C",
        note="The README: some scripts can only be combined with English, and an impossible combination is a 400. "
        "The real library refuses ch_sim with de before it loads a model.",
    ),
    Probe(
        id="easyocr.languages_empty_falls_back",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"languages": []},
        predicate=P.recall_at_least(0.85),
        tier="C",
        note="An empty list is not an error: the shim falls back to its default languages (English) without a "
        "message. A 200 that reads the English scan is the assertion. The recall is stored.",
    ),
    # --- decoders --------------------------------------------------------------------------------------
    *[
        Probe(
            id=f"easyocr.decoder_{decoder}",
            engine="easyocr",
            fixture="scan_clean.png",
            options={"decoder": decoder},
            predicate=P.recall_at_least(0.85),
            tier="C",
            note="Each decoder returns the clean tokens. No accuracy order between decoders is asserted.",
        )
        for decoder in ("greedy", "wordbeamsearch")
    ],
    Probe(
        id="easyocr.decoder_beamsearch_answers",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"decoder": "beamsearch"},
        predicate=P.ir_valid(),
        tier="C",
        note="beamsearch is accepted and returns a valid Document IR. Its recall is the next probe.",
    ),
    Probe(
        id="easyocr.decoder_beamsearch_recall",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"decoder": "beamsearch"},
        predicate=P.recall_at_least(0.85),
        gate="record",
        tier="C",
        note="Recorded, not gated: EasyOCR 1.7.2's own beam search cuts the end off every text piece longer than "
        "about 40 characters ('The quarterly inspection of the harb'), recall 0.67 on a clean scan. The bare "
        "library gives the same with beamWidth 5, 25 and 100, on CPU and on MPS, so the shim cannot fix it.",
    ),
    # --- text detection and rotation ---------------------------------------------------------------------
    Probe(
        id="easyocr.rotated_text_reads_sideways_page",
        engine="easyocr",
        fixture="scan_rotated90.png",
        options={"rotated_text": True},
        control={"rotated_text": False},
        predicate=P.recall_above_control(margin=0.25),
        tier="C",
        note="rotated_text makes a sideways page readable at all: 0.39 recall against 0.00. The plan's 80% is not "
        "reachable (the bare library, CPU or MPS, gives the same 0.39: the detector finds a sideways page as 42 "
        "pieces, and turning the page upright first gives 1.00). That number is stored by the next probe.",
    ),
    Probe(
        id="easyocr.rotated_text_sideways_page_plan_threshold",
        engine="easyocr",
        fixture="scan_rotated90.png",
        options={"rotated_text": True},
        predicate=P.recall_at_least(0.8),
        gate="record",
        tier="C",
        note="The plan's claim for a page rotated 90 degrees (recall of at least 80% with rotated_text). Recorded: "
        "EasyOCR 1.7.2 reaches 0.39.",
    ),
    Probe(
        id="easyocr.rotated_text_reads_upside_down_page",
        engine="easyocr",
        fixture="scan_upside_down.png",
        options={"rotated_text": True},
        control={"rotated_text": False},
        predicate=P.recall_pair(treatment_min=0.8, control_max=0.2),
        tier="C",
        note="Turned 180 degrees the page is detected as upright lines and rotated_text reads it (0.99 against 0.03).",
    ),
    Probe(
        id="easyocr.canvas_size_small_reads_less",
        engine="easyocr",
        fixture="large_page.png",
        options={"canvas_size": 2560},
        control={"canvas_size": 256},
        predicate=P.recall_above_control(margin=0.5, treatment_min=0.85),
        tier="B",
        note="An A3 page detected at 256 px is too small to find text. 2560 is the default.",
    ),
    # --- the shim's own options -------------------------------------------------------------------------
    Probe(
        id="easyocr.min_confidence_drops_text",
        engine="easyocr",
        fixture="scan_noisy.png",
        options={"min_confidence": 90},
        control={"min_confidence": 0},
        predicate=P.all_of("fewer_tokens_and_confident", fewer_tokens_than_control(), P.confidences_at_least(0.9)),
        tier="C",
        note="Text below 90% confidence is dropped, and every block that is left has a confidence of 0.9 or more. "
        "The plan's 'fewer blocks than with 0' is wrong for this engine: dropping pieces splits a paragraph "
        "(3 blocks with 90, 1 block with 0).",
    ),
    Probe(
        id="easyocr.paragraphs_group_lines",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"paragraphs": True},
        control={"paragraphs": False},
        predicate=P.fewer_blocks_than_control(),
        tier="C",
        note="Grouping lines gives fewer blocks than one block per line.",
    ),
    Probe(
        id="easyocr.headings_title_is_level_1",
        engine="easyocr",
        fixture="report_born_digital.pdf",
        options={"headings": True, "page_range": [1, 1]},
        control={"headings": False, "page_range": [1, 1]},
        predicate=headings_only_with_option(1),
        tier="C",
        note="With headings the page's title ('Harbour Warehouse Audit', level 1 in the truth) is a level 1 "
        "heading, and with headings false there is no heading block. The plan's 'the first block has level 1' "
        "is wrong for this fixture: its first block is the running page header.",
    ),
    Probe(
        id="easyocr.dpi_scales_pdf_pages",
        engine="easyocr",
        fixture="report_born_digital.pdf",
        options={"dpi": 300, "page_range": [1, 1]},
        control={"dpi": 72, "page_range": [1, 1]},
        predicate=P.all_of(
            "width_ratio_and_recall",
            page_width_ratio(300 / 72),
            P.recall_above_control(margin=0.0, pages=FIRST_PAGE),
        ),
        tier="C",
        note="The page width follows dpi, and the recall at 72 is no higher than at 300.",
    ),
    Probe(
        id="easyocr.dpi_ignored_for_png",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"dpi": 72},
        predicate=P.all_of("png_size_and_recall", page_size_is_truth(), P.recall_at_least(0.85)),
        tier="C",
        note="dpi renders PDF pages. A PNG keeps its own size.",
    ),
    Probe(
        id="easyocr.page_range_pdf",
        engine="easyocr",
        fixture="scan_multipage.pdf",
        options={"page_range": [2, 3]},
        predicate=P.all_of("window_pages_and_recall", pages_read({2, 3}), P.recall_at_least(0.8, pages={2, 3})),
        tier="C",
        note="Only pages 2 and 3 are read and they keep their own numbers.",
    ),
    Probe(
        id="easyocr.page_range_start_past_end",
        engine="easyocr",
        fixture="scan_multipage.pdf",
        options={"page_range": [9, 10]},
        expect_status=400,
        expect_code="invalid_options",
        tier="C",
        note="shimkit.raster: a range that starts after the last page is a 400.",
    ),
    Probe(
        id="easyocr.multipage_pdf",
        engine="easyocr",
        fixture="scan_multipage.pdf",
        predicate=P.all_of("pdf_pages_and_recall", pages_read({1, 2, 3, 4, 5}), P.recall_at_least(0.8)),
        tier="C",
        note="An image-only PDF is rendered and read page by page.",
    ),
    Probe(
        id="easyocr.include_page_images",
        engine="easyocr",
        fixture="scan_clean.png",
        options={"include_page_images": True},
        control={"include_page_images": False},
        predicate=P.page_images_match(exact=True),
        requires=("capability:page_images",),
        tier="C",
        note="pages[].image is a PNG of the page's size, so bbox is in image pixels. The control has none.",
    ),
    # --- image formats ---------------------------------------------------------------------------------
    *[
        Probe(
            id=f"easyocr.format_{ext}",
            engine="easyocr",
            fixture=f"scan_clean.{ext}",
            predicate=P.recall_at_least(0.85),
            tier="C",
            note=f"A {ext} image is read. (gif is refused: baseline.gif_unsupported.)",
        )
        for ext in ("jpg", "webp", "bmp")
    ],
    Probe(
        id="easyocr.page_range_tiff",
        engine="easyocr",
        fixture="multi_frame.tiff",
        options={"page_range": [2, 3]},
        predicate=P.all_of("tiff_window_and_recall", pages_read({2, 3}), P.recall_at_least(0.8, pages={2, 3})),
        tier="C",
        note="The README: page_range works for multi-page TIFFs, and the frames keep their own numbers.",
    ),
    Probe(
        id="easyocr.multi_frame_tiff",
        engine="easyocr",
        fixture="multi_frame.tiff",
        predicate=P.all_of("tiff_frames_and_recall", pages_read({1, 2, 3}), P.recall_at_least(0.8)),
        tier="C",
        note="Every frame of a TIFF is read as a page. The number of frames read is stored.",
    ),
]
