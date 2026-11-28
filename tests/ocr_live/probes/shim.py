"""Synthetic probes for the in-process shims of ``shims.py``: ``--engine shim=URL``. They check the runner and the
predicates, not an engine.

Run against the oracle (``shims.py oracle``) every probe passes. Against the null shim (``shims.py null``), which
ignores options, every paired probe fails. Against the garbage shim every recall probe fails.
"""

from __future__ import annotations

import predicates as P
from probe import Probe

PROBES = [
    Probe(
        id="shim.gif_unsupported",
        engine="shim",
        fixture="scan_clean.gif",
        expect_status=415,
        expect_code="unsupported_media_type",
        tier="A",
    ),
    # Paired: the option on against the option off on the same fixture.
    Probe(
        id="shim.ocr_off_reads_nothing",
        engine="shim",
        fixture="scan_clean.png",
        options={"ocr": True},
        control={"ocr": False},
        predicate=P.recall_pair(treatment_min=0.8, control_max=0.1),
        tier="B",
    ),
    Probe(
        id="shim.fast_reads_less",
        engine="shim",
        fixture="scan_german.png",
        options={"mode": "accurate"},
        control={"mode": "fast"},
        predicate=P.recall_above_control(margin=0.3, treatment_min=0.8),
        tier="C",
    ),
    Probe(
        id="shim.merge_makes_fewer_blocks",
        engine="shim",
        fixture="report_born_digital.pdf",
        options={"merge": True},
        control={"merge": False},
        predicate=P.fewer_blocks_than_control(),
        tier="C",
    ),
    # Not paired.
    Probe(
        id="shim.recall_chinese",
        engine="shim",
        fixture="scan_chinese.png",
        predicate=P.recall_at_least(0.8),
        tier="B",
    ),
    Probe(
        id="shim.recall_multipage_pdf",
        engine="shim",
        fixture="scan_multipage.pdf",
        predicate=P.all_of("pdf_recall_and_pages", P.recall_at_least(0.8), P.page_set_equals({1, 2, 3, 4, 5})),
        tier="C",
    ),
    Probe(
        id="shim.line_boxes",
        engine="shim",
        fixture="scan_clean.png",
        options={"granularity": "line"},
        predicate=P.line_boxes(min_iou=0.5, min_fraction=0.8),
        tier="C",
    ),
    Probe(
        id="shim.heading_levels",
        engine="shim",
        fixture="report_born_digital.pdf",
        predicate=P.distinct_heading_levels(3),
        tier="C",
    ),
    # A record probe: stores its numbers and never fails the run.
    Probe(
        id="shim.noisy_recall_recorded",
        engine="shim",
        fixture="scan_noisy.png",
        predicate=P.recall_at_least(0.99),
        gate="record",
        tier="C",
    ),
    # Needs a machine the runner cannot assume: skipped unless ``--has bin:...`` says it is met elsewhere.
    Probe(
        id="shim.needs_a_missing_program",
        engine="shim",
        fixture="scan_clean.png",
        predicate=P.recall_at_least(0.8),
        requires=("bin:ocr-live-no-such-program",),
        tier="C",
    ),
]
