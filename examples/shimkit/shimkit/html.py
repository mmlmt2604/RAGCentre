"""Small HTML helpers for engines that return HTML fragments (tables, lists, headings, maths).

Standard library only. The parsers are forgiving: unclosed tags, stray end tags and missing
``<tr>``/``<td>`` closers are tolerated, and nothing here raises on malformed input.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser

_SKIPPED = frozenset({"script", "style", "head", "template"})
_BLOCKS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "caption",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "tr",
        "ul",
    }
)
_MAX_SPAN = 1000  # the HTML specification's limit for colspan; also bounds rowspan


def _squash(text: str) -> str:
    return " ".join(text.split())


class _Parser(HTMLParser):
    """Skips ``<script>``/``<style>``/``<head>``, turns ``<math>x</math>`` into ``$x$`` and passes
    everything else to the ``start``, ``end`` and ``text`` hooks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._math: list[str] | None = None

    def start(self, tag: str, attrs: Mapping[str, str | None]) -> None: ...

    def end(self, tag: str) -> None: ...

    def text(self, data: str) -> None: ...

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skip += 1
        elif self._skip:
            return
        elif tag == "math":
            if self._math is None:
                self._math = []
        elif self._math is None:
            self.start(tag, dict(attrs))

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skip = max(0, self._skip - 1)
        elif self._skip:
            return
        elif tag == "math":
            self._end_math()
        elif self._math is None:
            self.end(tag)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._math is not None:
            self._math.append(data)
        else:
            self.text(data)

    def _end_math(self) -> None:
        if self._math is None:
            return
        latex = _squash("".join(self._math))
        self._math = None
        if latex:
            self.text(f"${latex}$")

    def finish(self) -> None:
        self.close()
        self._end_math()  # an unclosed <math>


# --- html_to_text -----------------------------------------------------------------------------


class _TextParser(_Parser):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self._current: list[str] = []
        self._pre = 0
        self._after_pre_start = False

    def _flush(self, *, keep_empty: bool = False) -> None:
        raw = "".join(self._current)
        self._current = []
        line = raw.rstrip() if self._pre else _squash(raw)
        if line or keep_empty:
            self.lines.append(line)

    def start(self, tag: str, attrs: Mapping[str, str | None]) -> None:
        if tag == "br":
            self._flush()
        elif tag in ("td", "th"):
            self._current.append(" ")
        elif tag in _BLOCKS:
            self._flush()
            if tag == "pre":
                self._pre += 1
                self._after_pre_start = True

    def end(self, tag: str) -> None:
        if tag in _BLOCKS:
            self._flush()
            if tag == "pre":
                self._pre = max(0, self._pre - 1)

    def text(self, data: str) -> None:
        if not self._pre:
            self._current.append(data)
            return
        if self._after_pre_start:  # a newline straight after <pre> is not content
            data = data.removeprefix("\n")
            self._after_pre_start = False
        first, *rest = data.split("\n")
        self._current.append(first)
        for part in rest:
            self._flush(keep_empty=True)
            self._current.append(part)


def html_to_text(html: str) -> str:
    """The visible text of an HTML fragment.

    Whitespace is collapsed, ``<br>`` and block-level boundaries become single newlines, table
    cells are separated by a space, ``<pre>`` keeps its own line breaks and indentation, and
    ``<math>x</math>`` becomes ``$x$``. Script, style and head content is dropped."""
    parser = _TextParser()
    parser.feed(html)
    parser.finish()
    parser._flush()
    lines = parser.lines
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# --- table_to_markdown ------------------------------------------------------------------------


@dataclass
class _Cell:
    colspan: int
    rowspan: int
    parts: list[str]


def _span(attrs: Mapping[str, str | None], key: str) -> int:
    try:
        return max(1, min(_MAX_SPAN, int(attrs.get(key) or 1)))
    except ValueError:
        return 1


class _TableParser(_Parser):
    """Collects the rows of the first ``<table>``. A table nested in a cell is flattened into
    that cell's text."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._depth = 0
        self.done = False

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append(self._cell)
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row is not None:
            self.rows.append(self._row)
        self._row = None

    def start(self, tag: str, attrs: Mapping[str, str | None]) -> None:
        if self.done:
            return
        if tag == "table":
            self._depth += 1
        if self._depth == 0:
            return
        if self._depth == 1 and tag == "tr":
            self._close_row()
            self._row = []
        elif self._depth == 1 and tag in ("td", "th"):
            self._close_cell()
            if self._row is None:
                self._row = []
            self._cell = _Cell(_span(attrs, "colspan"), _span(attrs, "rowspan"), [])
        elif self._cell is not None and (tag == "br" or tag in _BLOCKS or tag in ("td", "th")):
            self._cell.parts.append(" ")

    def end(self, tag: str) -> None:
        if self.done or self._depth == 0:
            return
        if tag == "table":
            self._depth -= 1
            if self._depth == 0:
                self._close_row()
                self.done = True
        elif self._depth == 1 and tag == "tr":
            self._close_row()
        elif self._depth == 1 and tag in ("td", "th"):
            self._close_cell()
        elif self._cell is not None and tag in _BLOCKS:
            self._cell.parts.append(" ")

    def text(self, data: str) -> None:
        if not self.done and self._cell is not None:
            self._cell.parts.append(data)

    def finish(self) -> None:
        super().finish()
        self._close_row()


def _grid(rows: list[list[_Cell]]) -> list[list[str]]:
    """Lay the cells out in a rectangle, repeating the text of a cell across the columns and rows
    it spans."""
    grid: list[list[str]] = []
    carried: dict[int, tuple[str, int]] = {}  # column -> (text, rows still to fill)
    for row in rows:
        out: list[str] = []

        def fill_carried(out: list[str] = out) -> None:
            while len(out) in carried:
                text, left = carried[len(out)]
                out.append(text)
                if left <= 1:
                    del carried[len(out) - 1]
                else:
                    carried[len(out) - 1] = (text, left - 1)

        for cell in row:
            fill_carried()
            text = _squash("".join(cell.parts))
            for _ in range(cell.colspan):
                carried.pop(len(out), None)  # a cell that overlaps a spanned column wins
                if cell.rowspan > 1:
                    carried[len(out)] = (text, cell.rowspan - 1)
                out.append(text)
        while any(column >= len(out) for column in carried):  # spanned cells right of the last cell
            if len(out) in carried:
                fill_carried()
            else:
                out.append("")
        grid.append(out)
    width = max((len(r) for r in grid), default=0)
    return [r + [""] * (width - len(r)) for r in grid]


def table_to_markdown(html: str) -> str:
    """The first ``<table>`` in ``html`` as a Markdown pipe table, or ``""`` if there is none.

    The first row is the header, whether its cells are ``<th>`` or ``<td>``. A cell with
    ``colspan`` or ``rowspan`` repeats its text in every cell it covers. ``|`` in a cell is
    escaped and line breaks become spaces."""
    parser = _TableParser()
    parser.feed(html)
    parser.finish()
    grid = _grid(parser.rows)
    if not grid or not grid[0]:
        return ""

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"

    return "\n".join([line(grid[0]), line(["---"] * len(grid[0])), *(line(r) for r in grid[1:])])


# --- split_list_items -------------------------------------------------------------------------


class _ListParser(_Parser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[list[str]] = []
        self._open: list[tuple[int, int]] = []  # (index into items, list depth) of each open <li>
        self._depth = 0

    def _add(self, data: str) -> None:
        if self._open:
            self.items[self._open[-1][0]].append(data)

    def start(self, tag: str, attrs: Mapping[str, str | None]) -> None:
        if tag in ("ul", "ol"):
            self._add(" ")
            self._depth += 1
        elif tag == "li":
            while self._open and self._open[-1][1] >= self._depth:  # an unclosed sibling
                self._open.pop()
            self.items.append([])
            self._open.append((len(self.items) - 1, self._depth))
        elif tag == "br" or tag in _BLOCKS:
            self._add(" ")

    def end(self, tag: str) -> None:
        if tag in ("ul", "ol"):
            self._depth = max(0, self._depth - 1)
            while self._open and self._open[-1][1] > self._depth:
                self._open.pop()
            self._add(" ")
        elif tag == "li":
            if self._open and self._open[-1][1] <= self._depth:
                self._open.pop()
        elif tag in _BLOCKS:
            self._add(" ")

    def text(self, data: str) -> None:
        self._add(data)


def split_list_items(html: str) -> list[str]:
    """The text of each ``<li>``, in document order. A nested list's items are separate entries
    (after the item that contains them), and are not repeated in their parent's text. Empty items
    are dropped."""
    parser = _ListParser()
    parser.feed(html)
    parser.finish()
    return [text for parts in parser.items if (text := _squash("".join(parts)))]


# --- first_heading_level ----------------------------------------------------------------------

_LEADING_HEADING = re.compile(r"\s*<h([1-6])(?![0-9A-Za-z])", re.IGNORECASE)


def first_heading_level(html: str) -> int | None:
    """``n`` when ``html`` starts with an ``<hn>`` tag (ignoring leading whitespace), else ``None``."""
    match = _LEADING_HEADING.match(html)
    return int(match.group(1)) if match else None
