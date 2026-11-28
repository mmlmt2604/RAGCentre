"""MinerU's model-free native documents and optional model-backed OCR tiers.

For a machine without MinerU models, select ``--probe 'mineru.*'``. The shared
baseline uses the service's standard tier, which needs those models.
"""

import predicates as P
from probe import Probe

FLASH = {"tier": "flash", "ocr": False}
MODELS = ("env:OCR_LIVE_MINERU_MODELS",)

PROBES = [
    Probe(
        id="mineru.unknown_option",
        engine="mineru",
        fixture="scan_clean.png",
        options={"no_such_option": True},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
    ),
    Probe(
        id="mineru.flash_pdf",
        engine="mineru",
        fixture="report_born_digital.pdf",
        options=FLASH,
        predicate=P.all_of("flash_pdf", P.ir_valid(), P.recall_at_least(0.85), P.boxes_inside_pages()),
        tier="B",
    ),
    *[
        Probe(
            id=f"mineru.native_{extension}",
            engine="mineru",
            fixture=f"sample.{extension}",
            options={"tier": "advanced"},
            predicate=P.all_of("native_document", P.ir_valid(), P.recall_at_least(0.8)),
            tier="B",
            note="Native formats always use flash, even when the route selects advanced.",
        )
        for extension in ("docx", "html", "csv")
    ],
    Probe(
        id="mineru.page_range",
        engine="mineru",
        fixture="report_born_digital.pdf",
        options={**FLASH, "page_range": "2-3"},
        predicate=P.page_set_equals({2, 3}),
        tier="C",
    ),
    Probe(
        id="mineru.page_range_reverse",
        engine="mineru",
        fixture="report_born_digital.pdf",
        options={**FLASH, "page_range": "r1"},
        predicate=P.page_set_equals({5}),
        tier="C",
    ),
    Probe(
        id="mineru.page_range_invalid",
        engine="mineru",
        fixture="report_born_digital.pdf",
        options={**FLASH, "page_range": "0-2"},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
    ),
    Probe(
        id="mineru.page_range_on_image",
        engine="mineru",
        fixture="scan_clean.png",
        options={**FLASH, "page_range": "1"},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
    ),
    Probe(
        id="mineru.ocr_standard",
        engine="mineru",
        fixture="scan_clean.png",
        options={"tier": "standard", "ocr": True},
        predicate=P.all_of("standard_ocr", P.ir_valid(), P.recall_at_least(0.8)),
        requires=MODELS,
        tier="C",
    ),
    *[
        Probe(
            id=f"mineru.ocr_{tier}",
            engine="mineru",
            fixture="scan_clean.png",
            options={"tier": tier, "ocr": True},
            predicate=P.all_of("tier_ocr", P.ir_valid(), P.recall_at_least(0.8)),
            requires=MODELS,
            tier="C",
        )
        for tier in ("basic", "advanced")
    ],
]
