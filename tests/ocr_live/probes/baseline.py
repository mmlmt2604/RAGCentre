"""Probes that hold for any engine that takes images (``engine="*"``). The runner loads this file for every engine.

Add an engine's own probes in ``probes/<engine>.py``. Do not repeat these there.
"""

from __future__ import annotations

import predicates as P
from probe import Probe

PROBES = [
    Probe(
        id="baseline.unknown_option",
        engine="*",
        fixture="scan_clean.png",
        options={"no_such_option": True},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
        note="A mistyped option must fail loudly. An engine that ignores it would hide every typo in a pipeline.",
    ),
    Probe(
        id="baseline.gif_unsupported",
        engine="*",
        fixture="scan_clean.gif",
        expect_status=415,
        expect_code="unsupported_media_type",
        tier="A",
        note="gif is not in the routing table of any engine, so the shim must refuse it rather than guess.",
    ),
    Probe(
        id="baseline.ir_valid",
        engine="*",
        fixture="scan_clean.png",
        predicate=P.ir_valid(),
        tier="B",
        note="The reply passes the host's normalise_ir, ids are unique, pages and boxes are in range.",
    ),
    Probe(
        id="baseline.clean_scan_recall",
        engine="*",
        fixture="scan_clean.png",
        predicate=P.recall_at_least(0.8),
        tier="B",
        note="A clean 200 dpi scan of an English paragraph is read with default options.",
    ),
]
