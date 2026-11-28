"""Probes for the EasyOCR shim (``examples/easyocr_shim``, port 8705): ``--engine easyocr=URL``.

EasyOCR is for many scripts and photographed text. It returns plain paragraphs with a confidence per
block. The rows follow the EasyOCR table of ``docs/plans/ocr-verification-plan.md``. Fixtures not named
here are in ``make_fixtures.py``.

Two probes need something a machine may lack and say so with ``requires``: the Chinese recognition model
(about 100 MB, downloaded on first use: set ``OCR_LIVE_ALLOW_MODEL_DOWNLOAD=1``) and the page images
capability of the shim.
"""

from __future__ import annotations

import predicates as P
from probe import Check, Observation, Probe, Verdict

NEEDS_DOWNLOAD = ("env:OCR_LIVE_ALLOW_MODEL_DOWNLOAD",)
FIRST_PAGE = {1}


# --- checks that only this engine's rows need -----------------------------------------------------


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
        predicate=P.recall_above_control(margin=0.01, treatment_min=0.8),
        tier="B",
        note="Plan row: German recall is higher with de than with en, and at least 80%.",
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
        for decoder in ("greedy", "beamsearch", "wordbeamsearch")
    ],
    # --- text detection and rotation ---------------------------------------------------------------------
    Probe(
        id="easyocr.rotated_text_reads_sideways_page",
        engine="easyocr",
        fixture="scan_rotated90.png",
        options={"rotated_text": True},
        control={"rotated_text": False},
        predicate=P.recall_above_control(margin=0.2, treatment_min=0.8),
        tier="C",
        note="Plan row: true recall is at least 80% and false recall is lower.",
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
        predicate=P.all_of("fewer_blocks_and_confident", P.fewer_blocks_than_control(), P.confidences_at_least(0.9)),
        tier="C",
        note="Plan row: fewer blocks than with 0, and every kept block has confidence 0.9 or more.",
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
        id="easyocr.headings_first_block",
        engine="easyocr",
        fixture="report_born_digital.pdf",
        options={"headings": True, "page_range": [1, 1]},
        predicate=P.first_block_heading(1),
        tier="C",
        note="Plan row: with headings the first block has level 1.",
    ),
    Probe(
        id="easyocr.headings_off_has_no_heading",
        engine="easyocr",
        fixture="report_born_digital.pdf",
        options={"headings": False, "page_range": [1, 1]},
        predicate=P.no_headings(),
        tier="C",
        note="Plan row: with headings false no heading block exists.",
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
        id="easyocr.multi_frame_tiff",
        engine="easyocr",
        fixture="multi_frame.tiff",
        predicate=P.all_of("tiff_frames_and_recall", pages_read({1, 2, 3}), P.recall_at_least(0.8)),
        tier="C",
        note="Every frame of a TIFF is read as a page. The number of frames read is stored.",
    ),
]
