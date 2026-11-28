"""Real Surya options, layout, and page geometry. Run with --engine surya=URL."""

import predicates as P
from probe import Check, Probe, Verdict


def layout():
    def check(obs):
        blocks = obs.run.body["blocks"]
        headings = [b for b in blocks if b["type"] == "heading"]
        tables = [b for b in blocks if b["type"] == "table" and b.get("html")]
        lists = [b for b in blocks if b["type"] == "list_item"]
        ok = bool(headings and tables and lists)
        return Verdict(
            ok,
            "headings, table HTML, and list items",
            {"headings": len(headings), "tables": len(tables), "list_items": len(lists)},
        )

    return Check("layout", "structure", check)


def figure_text():
    def check(obs):
        tokens = set(obs.truth.extras["figure_tokens"])

        def found(reply):
            text = " ".join(b["text"] for b in reply.body["blocks"] if b["type"] == "figure").lower()
            return sum(t in text.split() for t in tokens) / len(tokens)

        on, off = found(obs.run), found(obs.control)
        return Verdict(
            on >= 0.5 and on > off,
            "figure-contained text increases with ocr_figures",
            {"figure_recall_on": on, "figure_recall_off": off},
        )

    return Check("figure_text", "compare", check)


def dpi_ratio():
    def check(obs):
        high = obs.run.body["pages"][0]["width"]
        low = obs.control.body["pages"][0]["width"]
        ratio = high / low
        return Verdict(1.95 <= ratio <= 2.05, "192 dpi width is twice 96 dpi width", {"width_ratio": ratio})

    return Check("dpi_ratio", "geometry", check)


def confidence_scope():
    def check(obs):
        by_page = {}
        for block in obs.run.body["blocks"]:
            value = block.get("metadata", {}).get("confidence")
            if value is not None:
                by_page.setdefault(block.get("page"), set()).add(value)
        return Verdict(
            bool(by_page),
            "confidence values per page; Surya may share one score across a page",
            {"distinct_confidence_values_per_page": {str(k): len(v) for k, v in by_page.items()}},
        )

    return Check("confidence_scope", "structure", check)


PROBES = [
    Probe(
        id="surya.gif_unsupported",
        engine="surya",
        fixture="scan_clean.gif",
        expect_status=415,
        expect_code="unsupported_media_type",
        tier="A",
    ),
    Probe(
        id="surya.dpi_invalid",
        engine="surya",
        fixture="scan_clean.png",
        options={"dpi": 71},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
    ),
    Probe(
        id="surya.defaults",
        engine="surya",
        fixture="scan_clean.png",
        predicate=P.all_of("defaults", P.recall_at_least(0.9), P.boxes_inside_pages(), P.confidence_in_range()),
        tier="B",
    ),
    Probe(
        id="surya.confidence_scope",
        engine="surya",
        fixture="scan_clean.png",
        predicate=confidence_scope(),
        gate="record",
        tier="B",
    ),
    Probe(id="surya.pdf_layout", engine="surya", fixture="report_born_digital.pdf", predicate=layout(), tier="C"),
    Probe(
        id="surya.ocr_figures",
        engine="surya",
        fixture="figure_with_text.pdf",
        options={"ocr_figures": True},
        control={"ocr_figures": False},
        predicate=figure_text(),
        tier="C",
    ),
    Probe(
        id="surya.dpi",
        engine="surya",
        fixture="report_born_digital.pdf",
        options={"dpi": 192, "page_range": [1, 1]},
        control={"dpi": 96, "page_range": [1, 1]},
        predicate=dpi_ratio(),
        tier="C",
    ),
    Probe(
        id="surya.page_images",
        engine="surya",
        fixture="scan_clean.png",
        options={"include_page_images": True},
        predicate=P.page_images_match(),
        tier="C",
    ),
    Probe(
        id="surya.page_range",
        engine="surya",
        fixture="report_born_digital.pdf",
        options={"page_range": [2, 3]},
        predicate=P.page_set_equals({2, 3}),
        tier="C",
    ),
    Probe(
        id="surya.page_range_clamp",
        engine="surya",
        fixture="report_born_digital.pdf",
        options={"page_range": [5, 9]},
        predicate=P.page_set_equals({5}),
        tier="C",
    ),
    Probe(
        id="surya.page_range_past_end",
        engine="surya",
        fixture="report_born_digital.pdf",
        options={"page_range": [8, 9]},
        expect_status=400,
        expect_code="invalid_options",
        tier="C",
    ),
    Probe(
        id="surya.page_range_on_image",
        engine="surya",
        fixture="scan_clean.png",
        options={"page_range": [1, 1]},
        expect_status=400,
        expect_code="invalid_options",
        tier="B",
    ),
]
