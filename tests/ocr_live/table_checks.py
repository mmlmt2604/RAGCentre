"""Check table content and cell positions against fixture truth."""

import html
import re

import predicates as P
from probe import Check, Observation, Verdict

_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_PIPE_ROW = re.compile(r"^\|.*\|\s*$", re.M)
_PIPE_RULE = re.compile(r"^\|[\s:|-]*-{3}[\s:|-]*\|\s*$", re.M)


def html_rows(markup: str) -> list[list[str]]:
    """Cell text by row, with tags removed, entities decoded, and whitespace collapsed."""
    return [
        [" ".join(html.unescape(_TAG.sub("", cell)).split()) for cell in _CELL.findall(row)]
        for row in _ROW.findall(markup)
    ]


def table_cells(min_share: float = 1.0) -> Check:
    """Require Markdown and HTML with the expected cells in their original rows and columns."""

    def fn(obs: Observation) -> Verdict:
        wanted = obs.truth.extras["table"]["cells"]
        total = sum(len(row) for row in wanted)
        tables = [b for b in P.blocks_of(obs.run.body) if b.get("type") == "table"]
        if not tables:
            return Verdict(False, "no table block", {"tables": 0})
        best = None
        for block in tables:
            rows = html_rows(str(block.get("html", "")))
            hits = sum(
                1
                for r, row in enumerate(wanted)
                for c, cell in enumerate(row)
                if r < len(rows) and c < len(rows[r]) and rows[r][c] == cell
            )
            if best is None or hits > best[0]:
                best = (hits, sum(len(row) for row in rows), block)
        assert best is not None
        hits, cells, block = best
        text = str(block.get("text", ""))
        problems = []
        if not (_PIPE_ROW.search(text) and _PIPE_RULE.search(text)):
            problems.append("text is not a Markdown table")
        if not block.get("html"):
            problems.append("no html field")
        if cells != total:
            problems.append(f"html has {cells} cells, the fixture's table {total}")
        share = hits / total if total else 1.0
        if share < min_share:
            problems.append(f"{hits} of {total} cells are in the right place, needs {min_share:.0%}")
        return Verdict(
            not problems,
            "; ".join(problems),
            {
                "tables": len(tables),
                "html_cells": cells,
                "cells_exact": round(share, 4),
                "cell_texts": html_rows(str(block.get("html", ""))),
            },
        )

    return Check("table_cells", "structure", fn)
