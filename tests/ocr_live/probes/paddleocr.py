"""PaddleOCR table, layout, orientation, and conditional model probes."""

import re

import predicates as P
from probe import Check, Probe, Verdict
from table_checks import table_cells

_LATEX = re.compile(r"\\(frac|sqrt|pm|mathrm|left|right)\b")


def tables(enabled):
    def check(obs):
        html = [b for b in obs.run.body["blocks"] if b["type"] == "table" and b.get("html")]
        if obs.control is None:
            ok = bool(html) if enabled else not html
        else:
            off = [b for b in obs.control.body["blocks"] if b["type"] == "table" and b.get("html")]
            ok = bool(html) and not off
        content = table_cells(0.75)(obs) if enabled else None
        return Verdict(
            ok and (content is None or content.ok),
            "table HTML follows the tables option" + (f"; {content.detail}" if content else ""),
            {"html_tables": len(html), **(content.numbers if content else {})},
        )

    return Check("table_html", "compare" if enabled else "structure", check)


def list_items():
    def check(obs):
        count = sum(b["type"] == "list_item" for b in obs.run.body["blocks"])
        control = sum(b["type"] == "list_item" for b in obs.control.body["blocks"])
        return Verdict(
            count > control, "list heuristic adds list-item blocks", {"list_items": count, "control": control}
        )

    return Check("list_items", "compare", check)


def formulas():
    """Formula recognition returns LaTeX commands; without it the formula is detected but not structured."""

    def check(obs):
        def latex(body):
            return [b for b in P.blocks_of(body) if b.get("type") == "formula" and _LATEX.search(b.get("text", ""))]

        on = latex(obs.run.body)
        off = latex(obs.control.body)
        return Verdict(
            bool(on) and not off,
            "formula recognition adds LaTeX structure",
            {"formulas_on": len(on), "formulas_off": len(off)},
        )

    return Check("formula_latex", "compare", check)


def reading_order():
    """``orientation`` restores forward reading order; the control reads the lines back to front.

    Token recall cannot see this difference: both arms recognise the same words. Compare the words that
    lead each reply instead. The forward reply begins with the truth's first line; the unoriented reply
    begins with the truth's last line (its lines are reversed, each line still forward)."""

    def check(obs):
        def leading(body):
            text = " ".join(b["text"] for b in P.blocks_of(body))
            return P.tokenize(text)[:LEAD]

        truth = obs.truth.tokens()
        run_leads = leading(obs.run.body)
        control_leads = leading(obs.control.body)
        forward = bool(truth) and run_leads == list(truth[:LEAD])
        reversed_control = bool(truth) and control_leads == list(truth[-LEAD:])
        return Verdict(
            forward and reversed_control,
            "orientation restores forward reading order while the control reads it back to front",
            {"run_leads": run_leads, "control_leads": control_leads},
        )

    return Check("reading_order", "compare", check)


LEAD = 9


PROBES = [
    Probe(
        id="paddleocr.gif_unsupported",
        engine="paddleocr",
        fixture="scan_clean.gif",
        expect_status=415,
        expect_code="unsupported_media_type",
        tier="A",
    ),
    Probe(
        id="paddleocr.bad_limit",
        engine="paddleocr",
        fixture="scan_clean.png",
        options={"text_det_limit_side_len": 1},
        expect_status=400,
        expect_code="invalid_options",
        tier="A",
    ),
    Probe(id="paddleocr.table_scan", engine="paddleocr", fixture="scan_table.png", predicate=tables(True), tier="B"),
    Probe(
        id="paddleocr.tables_toggle",
        engine="paddleocr",
        fixture="scan_table.png",
        options={"tables": True},
        control={"tables": False},
        predicate=tables(True),
        tier="B",
    ),
    Probe(
        id="paddleocr.orientation_180",
        engine="paddleocr",
        fixture="scan_upside_down.png",
        options={"orientation": True},
        control={"orientation": False},
        predicate=reading_order(),
        tier="C",
    ),
    Probe(
        id="paddleocr.orientation_90",
        engine="paddleocr",
        fixture="scan_rotated90.png",
        options={"orientation": True},
        control={"orientation": False},
        predicate=reading_order(),
        tier="C",
    ),
    Probe(
        id="paddleocr.formulas",
        engine="paddleocr",
        fixture="report_born_digital.pdf",
        options={"formulas": True},
        control={"formulas": False},
        predicate=formulas(),
        tier="C",
    ),
    Probe(
        id="paddleocr.list_heuristic",
        engine="paddleocr",
        fixture="report_born_digital.pdf",
        options={"list_heuristic": True},
        control={"list_heuristic": False},
        predicate=list_items(),
        gate="record",
        tier="C",
        note="Paddle's text layer drops the PDF's bullet markers, so the heuristic has no paragraph to transform here; its logic is unit-tested. No scanned-list fixture exercises it live.",
    ),
    Probe(
        id="paddleocr.heading_levels",
        engine="paddleocr",
        fixture="report_born_digital.pdf",
        options={"heading_levels": True},
        predicate=P.distinct_heading_levels(2),
        tier="C",
    ),
    Probe(
        id="paddleocr.unwarp",
        engine="paddleocr",
        fixture="scan_warped.png",
        options={"unwarp": True},
        control={"unwarp": False},
        predicate=P.recall_above_control(margin=0.01, treatment_min=0.8),
        gate="record",
        tier="C",
        note="The synthetic fixture is already readable without unwarping.",
    ),
    Probe(
        id="paddleocr.lang_german",
        engine="paddleocr",
        fixture="scan_german.png",
        options={"lang": "german"},
        predicate=P.recall_at_least(0.8),
        requires=("env:OCR_LIVE_PADDLE_EXTRA_MODELS",),
        tier="C",
        note="Needs a separate recognition model; do not download under the current disk budget.",
    ),
    Probe(
        id="paddleocr.charts",
        engine="paddleocr",
        fixture="figure_with_text.pdf",
        options={"charts": True},
        predicate=P.ir_valid(),
        requires=("env:OCR_LIVE_PADDLE_EXTRA_MODELS",),
        tier="C",
    ),
    Probe(
        id="paddleocr.seals",
        engine="paddleocr",
        fixture="scan_clean.png",
        options={"seals": True},
        predicate=P.ir_valid(),
        requires=("env:OCR_LIVE_PADDLE_EXTRA_MODELS",),
        tier="C",
        note="No seal ground truth is provided; this checks loading only, not seal recognition accuracy.",
    ),
    Probe(
        id="paddleocr.vl",
        engine="paddleocr",
        fixture="scan_table.png",
        options={"pipeline": "vl"},
        predicate=tables(True),
        requires=("env:OCR_LIVE_PADDLE_VL",),
        tier="C",
    ),
]
