"""Probes for the docTR shim (``examples/doctr_shim``, port 8706): ``--engine doctr=http://127.0.0.1:8706``.

docTR reads words and lines with tight boxes on Latin script and does no layout analysis. The shim groups lines into
paragraphs and, with ``headings``, guesses headings from the line height, so a probe on structure asks for what that
guess can do and records what it cannot.

Model sweep. ``detection`` and ``recognition`` each list 8 architectures. Every architecture is downloaded on first
use (about 100 to 200 MB for a detection and recognition pair). A tier C run sweeps the default of each plus one more of
each family (``DOWNLOADED`` below). The rest need ``OCR_LIVE_DOCTR_ALL_MODELS=1`` and room for about 1 GB of models;
without it they are reported as ``skipped``, not as passed.

Not covered, because no fixture holds the text: Cyrillic (the README says Latin script works best) and handwriting.
Chinese is recorded as a limit.
"""

from __future__ import annotations

import predicates as P
from probe import Check, Observation, Probe, Verdict

ALL_MODELS = ("env:OCR_LIVE_DOCTR_ALL_MODELS",)
DETECTION = [
    "fast_base",
    "fast_small",
    "fast_tiny",
    "db_resnet50",
    "db_mobilenet_v3_large",
    "linknet_resnet18",
    "linknet_resnet34",
    "linknet_resnet50",
]
RECOGNITION = [
    "crnn_vgg16_bn",
    "crnn_mobilenet_v3_small",
    "crnn_mobilenet_v3_large",
    "parseq",
    "master",
    "sar_resnet31",
    "vitstr_small",
    "vitstr_base",
]
DOWNLOADED = {
    "detection": {"fast_base", "db_resnet50", "linknet_resnet18"},
    "recognition": {"crnn_vgg16_bn", "parseq"},
}


# --- checks that only docTR's options need --------------------------------------------------------


def page_width_ratio(expected: float, tolerance: float = 0.05) -> Check:
    """The run's page is ``expected`` times as wide as the control's, within ``tolerance`` (a share of ``expected``)."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "page_width_ratio needs a control run"
        run, control = P.page_dims(obs.run.body), P.page_dims(obs.control.body)
        if not run or not control:
            return Verdict(False, "a result declares no page size", {})
        width, control_width = run[min(run)][0], control[min(control)][0]
        ratio = width / control_width
        return Verdict(
            abs(ratio - expected) <= tolerance * expected,
            f"page width {width:g} against {control_width:g}: ratio {ratio:.3f}, expected {expected:.3f} within {tolerance:.0%}",
            {"width": width, "control_width": control_width, "width_ratio": round(ratio, 4)},
        )

    return Check("page_width_ratio", "compare", fn)


def page_size_is_the_image() -> Check:
    """The first page is as large as the fixture's own pixels: nothing resampled the image."""

    def fn(obs: Observation) -> Verdict:
        want = obs.truth.pages[0]
        got = P.page_dims(obs.run.body).get(1)
        ok = got is not None and abs(got[0] - want.width) <= 1 and abs(got[1] - want.height) <= 1
        return Verdict(ok, f"page {got}, the image is {want.width:g}x{want.height:g}", {"page": list(got or [])})

    return Check("page_size_is_the_image", "geometry", fn)


def one_block_per_truth_line() -> Check:
    """With paragraphs off the run has as many blocks as the fixture has lines, and more than with them on."""

    def fn(obs: Observation) -> Verdict:
        assert obs.control is not None, "one_block_per_truth_line needs a control run"
        lines = sum(len(p.lines) for p in obs.truth.pages)
        blocks, grouped = len(P.blocks_of(obs.run.body)), len(P.blocks_of(obs.control.body))
        return Verdict(
            blocks == lines and blocks > grouped,
            f"{blocks} blocks with paragraphs off (the fixture has {lines} lines), {grouped} with them on",
            {"blocks": blocks, "control_blocks": grouped, "truth_lines": lines},
        )

    return Check("one_block_per_truth_line", "compare", fn)


def title_is_heading(level: int = 1) -> Check:
    """The fixture's first heading of ``level`` (its title) comes back as a heading of that level, and the control
    run, which has headings off, has no heading block at all."""

    def fn(obs: Observation) -> Verdict:
        title = next(b for p in obs.truth.pages for b in p.blocks if b.type == "heading" and b.level == level)
        want = P.tokenize(title.text)
        hits = [
            b
            for b in P.blocks_of(obs.run.body)
            if b.get("type") == "heading"
            and b.get("level") == level
            and P.recall(want, P.tokenize(str(b.get("text", "")))) >= P.RATIO
        ]
        found, without = P.heading_levels(obs.run.body), P.heading_levels(obs.control.body) if obs.control else []
        return Verdict(
            bool(hits) and not without,
            f"{len(hits)} level {level} headings read {title.text!r}; heading levels {sorted(set(found))}; "
            f"{len(without)} headings with the option off",
            {"title_found": bool(hits), "headings": len(found), "control_headings": len(without)},
        )

    return Check(f"title_is_heading_{level}", "structure", fn)


def page_count_is(count: int) -> Check:
    """``metadata.page_count`` and the declared pages both say ``count``."""

    def fn(obs: Observation) -> Verdict:
        meta = obs.run.body.get("metadata") if isinstance(obs.run.body, dict) else None
        declared = len(P.declared_pages(obs.run.body))
        said = (meta or {}).get("page_count")
        return Verdict(
            said == count and declared == count,
            f"metadata.page_count {said}, {declared} pages declared, expected {count}",
            {"page_count": said},
        )

    return Check(f"page_count_is_{count}", "structure", fn)


# --- the table ------------------------------------------------------------------------------------

PROBES: list[Probe] = [
    # Errors the shim answers without reading anything (tier A).
    Probe(
        id="doctr.bad_detection_name",
        engine="doctr",
        fixture="scan_clean.png",
        options={"detection": "no_such_model"},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
        note="The choice list is checked before any model loads.",
    ),
    Probe(
        id="doctr.bad_recognition_name",
        engine="doctr",
        fixture="scan_clean.png",
        options={"recognition": "no_such_model"},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
    ),
    Probe(
        id="doctr.dpi_below_range",
        engine="doctr",
        fixture="scan_clean.png",
        options={"dpi": 71},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
    ),
    Probe(
        id="doctr.dpi_above_range",
        engine="doctr",
        fixture="scan_clean.png",
        options={"dpi": 401},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
    ),
    Probe(
        id="doctr.min_confidence_above_range",
        engine="doctr",
        fixture="scan_clean.png",
        options={"min_confidence": 101},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
    ),
    Probe(
        id="doctr.docx_unsupported",
        engine="doctr",
        fixture="sample.docx",
        expect_status=415,
        expect_code="unsupported_media_type",
        tier="A",
        note="docTR reads PDF and images only. The gif case is in baseline.",
    ),
    Probe(
        id="doctr.html_unsupported",
        engine="doctr",
        fixture="sample.html",
        expect_status=415,
        expect_code="unsupported_media_type",
        tier="A",
    ),
    # Default options (tier B).
    Probe(
        id="doctr.defaults",
        engine="doctr",
        fixture="scan_clean.png",
        predicate=P.all_of(
            "defaults",
            P.recall_at_least(0.9),
            P.boxes_inside_pages(require_boxes=True),
            P.confidence_in_range(require_all=True),
        ),
        tier="B",
        note="Recall 0.9 and a box and a confidence on every block.",
    ),
    Probe(
        id="doctr.line_boxes",
        engine="doctr",
        fixture="scan_clean.png",
        options={"paragraphs": False},
        predicate=P.line_boxes(min_iou=0.5, min_fraction=0.8),
        tier="B",
        note="Measured with paragraphs off: with them on, the seven lines are one block and no line box exists to compare.",
    ),
    # Two option probes in tier B.
    Probe(
        id="doctr.paragraphs",
        engine="doctr",
        fixture="scan_clean.png",
        options={"paragraphs": False},
        control={"paragraphs": True},
        predicate=one_block_per_truth_line(),
        tier="B",
    ),
    Probe(
        id="doctr.straight_pages.rotated90",
        engine="doctr",
        fixture="scan_rotated90.png",
        options={"straight_pages": False},
        control={"straight_pages": True},
        predicate=P.recall_above_control(margin=0.5, treatment_min=0.8),
        tier="B",
        note="A page turned 90 degrees is unreadable with straight_pages on. The plan only names the 12 degree scan.",
    ),
    # The rest of the matrix (tier C).
    Probe(
        id="doctr.pdf_defaults",
        engine="doctr",
        fixture="report_born_digital.pdf",
        predicate=P.all_of(
            "pdf_defaults",
            P.ir_valid(),
            P.recall_at_least(0.9),
            P.boxes_inside_pages(require_boxes=True),
            P.confidence_in_range(require_all=True),
        ),
        tier="C",
    ),
    Probe(
        id="doctr.straight_pages.skew12",
        engine="doctr",
        fixture="scan_skew12.png",
        options={"straight_pages": False},
        control={"straight_pages": True},
        predicate=P.recall_above_control(margin=0.02),
        gate="record",
        tier="C",
        note="Recorded: the plan expects no loss. The gain probe needs a margin, so equal recall reads as not met.",
    ),
    Probe(
        id="doctr.dpi.pdf_width",
        engine="doctr",
        fixture="report_born_digital.pdf",
        options={"dpi": 300},
        control={"dpi": 72},
        predicate=P.all_of("dpi_scales_the_page", page_width_ratio(300 / 72), P.recall_at_least(0.9)),
        tier="C",
        note="The page is dpi/72 points wide, so 300 against 72 is 4.17 times.",
    ),
    Probe(
        id="doctr.dpi.pdf_recall_direction",
        engine="doctr",
        fixture="report_born_digital.pdf",
        options={"dpi": 300},
        control={"dpi": 72},
        predicate=P.recall_above_control(margin=0.005),
        gate="record",
        tier="C",
        note="Recorded: a 72 dpi render of this fixture is already read in full, so 300 dpi cannot read more.",
    ),
    Probe(
        id="doctr.dpi.png_ignored",
        engine="doctr",
        fixture="scan_clean.png",
        options={"dpi": 72},
        predicate=P.all_of("dpi_ignored_for_images", page_size_is_the_image(), P.recall_at_least(0.9)),
        tier="C",
        note="dpi renders PDF pages only: an image keeps its own size.",
    ),
    Probe(
        id="doctr.headings",
        engine="doctr",
        fixture="report_born_digital.pdf",
        options={"headings": True, "paragraphs": False},
        control={"headings": False, "paragraphs": False},
        predicate=title_is_heading(1),
        tier="C",
        note="The title is the first block only if the page header is ignored, so the probe looks for the title's text.",
    ),
    Probe(
        id="doctr.headings.default_grouping",
        engine="doctr",
        fixture="report_born_digital.pdf",
        options={"headings": True},
        control={"headings": False},
        predicate=title_is_heading(1),
        gate="record",
        tier="C",
        note="Recorded limit: docTR's boxes follow the ink, so the title is not much taller than the line under it, joins "
        "that paragraph, and is then not short enough to be a heading. See the README.",
    ),
    Probe(
        id="doctr.min_confidence",
        engine="doctr",
        fixture="scan_noisy.png",
        options={"min_confidence": 99, "paragraphs": False},
        control={"min_confidence": 0, "paragraphs": False},
        predicate=P.all_of("min_confidence_drops_lines", P.fewer_blocks_than_control(), P.confidences_at_least(0.99)),
        tier="C",
        note="The plan's 90 drops nothing here: docTR is at least 97% sure of every line of the noisy scan.",
    ),
    Probe(
        id="doctr.min_confidence.p90",
        engine="doctr",
        fixture="scan_noisy.png",
        options={"min_confidence": 90},
        control={"min_confidence": 0},
        predicate=P.all_of("min_confidence_90", P.fewer_blocks_than_control(), P.confidences_at_least(0.9)),
        gate="record",
        tier="C",
        note="The plan's literal probe. Recorded because the fixture has no line below 90.",
    ),
    Probe(
        id="doctr.page_images",
        engine="doctr",
        fixture="scan_clean.png",
        options={"include_page_images": True},
        control={"include_page_images": False},
        predicate=P.page_images_match(exact=True),
        tier="C",
        note="exact: the image is the page's size, so bbox is in image pixels.",
    ),
    Probe(
        id="doctr.page_images.pdf",
        engine="doctr",
        fixture="scan_multipage.pdf",
        options={"include_page_images": True, "page_range": [2, 3]},
        control={"page_range": [2, 3]},
        predicate=P.page_images_match(exact=True),
        tier="C",
    ),
    Probe(
        id="doctr.page_range.pdf",
        engine="doctr",
        fixture="report_born_digital.pdf",
        options={"page_range": [2, 3]},
        predicate=P.all_of(
            "window",
            page_count_is(2),
            P.page_set_equals({2, 3}),
            P.recall_at_least(0.9, pages={2, 3}),
        ),
        tier="C",
        note="Page numbers stay the original's, and page_count is the window's size.",
    ),
    Probe(
        id="doctr.page_range.past_end",
        engine="doctr",
        fixture="report_born_digital.pdf",
        options={"page_range": [9, 10]},
        expect_status=400,
        expect_code="invalid_options",
        tier="B",
        note="A range that starts after the last page is refused, not an empty result.",
    ),
    Probe(
        id="doctr.page_range.image",
        engine="doctr",
        fixture="scan_clean.png",
        options={"page_range": [2, 3]},
        expect_status=400,
        expect_code="invalid_options",
        tier="B",
    ),
    Probe(
        id="doctr.tiff_frames",
        engine="doctr",
        fixture="multi_frame.tiff",
        predicate=P.all_of("three_frames", page_count_is(3), P.page_set_equals({1, 2, 3}), P.recall_at_least(0.9)),
        tier="C",
    ),
    Probe(
        id="doctr.tiff_page_range",
        engine="doctr",
        fixture="multi_frame.tiff",
        options={"page_range": [2, 3]},
        predicate=P.all_of("frame_window", P.page_set_equals({2, 3}), P.recall_at_least(0.9, pages={2, 3})),
        tier="C",
    ),
    Probe(
        id="doctr.format.jpg",
        engine="doctr",
        fixture="scan_clean.jpg",
        predicate=P.recall_at_least(0.8),
        tier="C",
    ),
    Probe(
        id="doctr.format.webp",
        engine="doctr",
        fixture="scan_clean.webp",
        predicate=P.recall_at_least(0.8),
        tier="C",
    ),
    Probe(
        id="doctr.format.bmp",
        engine="doctr",
        fixture="scan_clean.bmp",
        predicate=P.recall_at_least(0.8),
        tier="C",
    ),
    Probe(
        id="doctr.chinese",
        engine="doctr",
        fixture="scan_chinese.png",
        predicate=P.recall_at_least(0.5),
        gate="record",
        tier="C",
        note="A limit, not a fault: the default recognition vocabulary is Latin script. The number is the finding.",
    ),
]

# One probe per architecture: recall of 0.7 on the clean scan with the other model left at its default.
for _arch in DETECTION:
    PROBES.append(
        Probe(
            id=f"doctr.detection.{_arch}",
            engine="doctr",
            fixture="scan_clean.png",
            options={"detection": _arch},
            predicate=P.recall_at_least(0.7),
            tier="C",
            requires=() if _arch in DOWNLOADED["detection"] else ALL_MODELS,
        )
    )
for _arch in RECOGNITION:
    PROBES.append(
        Probe(
            id=f"doctr.recognition.{_arch}",
            engine="doctr",
            fixture="scan_clean.png",
            options={"recognition": _arch},
            predicate=P.recall_at_least(0.7),
            tier="C",
            requires=() if _arch in DOWNLOADED["recognition"] else ALL_MODELS,
        )
    )
