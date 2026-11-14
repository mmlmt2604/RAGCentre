from __future__ import annotations

import pytest

from shimkit.html import first_heading_level, html_to_text, split_list_items, table_to_markdown

# --- html_to_text -----------------------------------------------------------------------------


def test_text_collapses_whitespace_and_decodes_entities() -> None:
    assert html_to_text("<p>  Flow \n  rate &amp; <b>head</b>\t</p>") == "Flow rate & head"


def test_text_block_boundaries_and_br_become_newlines() -> None:
    html = "<h1>Title</h1><p>one<br>two<br/><br/>three</p><div>four</div>five"
    assert html_to_text(html) == "Title\none\ntwo\nthree\nfour\nfive"


def test_text_inline_tags_do_not_break_lines() -> None:
    assert html_to_text("<p>a<b>b</b><i>c</i> <span>d</span></p>") == "abc d"


def test_text_math_becomes_dollars() -> None:
    assert html_to_text("<p>Energy <math>E = mc^2</math> holds</p>") == "Energy $E = mc^2$ holds"
    assert html_to_text("<math>\\frac{a}{b}\n + c</math>") == "$\\frac{a}{b} + c$"
    assert html_to_text("<math>  </math>") == ""
    assert html_to_text("<math>a &lt; b</math>") == "$a < b$"


def test_text_tags_inside_math_are_ignored() -> None:
    assert html_to_text("<math><mi>x</mi><mo>+</mo><mn>1</mn></math>") == "$x+1$"


def test_text_unclosed_math_is_closed() -> None:
    assert html_to_text("<p>a <math>x^2") == "a $x^2$"


def test_text_drops_script_style_and_head() -> None:
    html = "<head><title>T</title></head><style>p{}</style><p>keep</p><script>alert(1)</script>"
    assert html_to_text(html) == "keep"


def test_text_pre_keeps_lines_and_indentation() -> None:
    html = "<p>Before</p><pre><code>def f(x):\n    return x\n\n\nprint(f(1))\n</code></pre><p>After</p>"
    assert html_to_text(html) == "Before\ndef f(x):\n    return x\n\n\nprint(f(1))\nAfter"
    assert html_to_text("<pre>\nfirst\nsecond</pre>") == "first\nsecond"


def test_text_table_cells_are_separated() -> None:
    assert html_to_text("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>") == "a b\nc d"


def test_text_plain_and_empty_input() -> None:
    assert html_to_text("just text") == "just text"
    assert html_to_text("") == ""
    assert html_to_text("<p></p><br>") == ""


@pytest.mark.parametrize("html", ["<p>unclosed <b>bold", "</div></p>stray", "<<>>", "<p a=>x", "<!-- c -->y"])
def test_text_survives_malformed_html(html: str) -> None:
    assert isinstance(html_to_text(html), str)


# --- table_to_markdown ------------------------------------------------------------------------


def test_table_basic_with_th_header() -> None:
    html = "<table><tr><th>Flow</th><th>Head</th></tr><tr><td>40 l/min</td><td>12 m</td></tr></table>"
    assert table_to_markdown(html) == "| Flow | Head |\n| --- | --- |\n| 40 l/min | 12 m |"


def test_table_header_less_uses_first_row_as_header() -> None:
    html = "<table><tbody><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></tbody></table>"
    assert table_to_markdown(html) == "| a | b |\n| --- | --- |\n| 1 | 2 |"


def test_table_thead_tbody_tfoot_in_document_order() -> None:
    html = (
        "<table><thead><tr><th>h</th></tr></thead><tbody><tr><td>b</td></tr></tbody>"
        "<tfoot><tr><td>f</td></tr></tfoot></table>"
    )
    assert table_to_markdown(html) == "| h |\n| --- |\n| b |\n| f |"


def test_table_colspan_repeats_text() -> None:
    html = '<table><tr><th colspan="2">Pump</th><th>Motor</th></tr><tr><td>A</td><td>B</td><td>C</td></tr></table>'
    assert table_to_markdown(html) == "| Pump | Pump | Motor |\n| --- | --- | --- |\n| A | B | C |"


def test_table_rowspan_repeats_text_down() -> None:
    html = (
        '<table><tr><th rowspan="2">Model</th><th>Flow</th></tr>'
        "<tr><td>40</td></tr><tr><td>X2</td><td>50</td></tr></table>"
    )
    assert table_to_markdown(html) == "| Model | Flow |\n| --- | --- |\n| Model | 40 |\n| X2 | 50 |"


def test_table_rowspan_in_the_middle_and_at_the_end_of_a_row() -> None:
    html = (
        "<table><tr><td>a</td><td rowspan='2'>b</td><td>c</td></tr>"
        "<tr><td>d</td><td>e</td></tr>"
        "<tr><td rowspan='2'>f</td><td>g</td><td>h</td></tr><tr><td>i</td><td>j</td></tr></table>"
    )
    assert table_to_markdown(html).splitlines() == [
        "| a | b | c |",
        "| --- | --- | --- |",
        "| d | b | e |",
        "| f | g | h |",
        "| f | i | j |",
    ]


def test_table_rowspan_beyond_last_cell_of_a_row() -> None:
    html = "<table><tr><td>a</td><td rowspan='2'>b</td></tr><tr><td>c</td></tr></table>"
    assert table_to_markdown(html).splitlines() == ["| a | b |", "| --- | --- |", "| c | b |"]


def test_table_colspan_and_rowspan_together() -> None:
    html = (
        "<table><tr><td colspan='2' rowspan='2'>big</td><td>x</td></tr><tr><td>y</td></tr>"
        "<tr><td>1</td><td>2</td><td>3</td></tr></table>"
    )
    assert table_to_markdown(html).splitlines() == [
        "| big | big | x |",
        "| --- | --- | --- |",
        "| big | big | y |",
        "| 1 | 2 | 3 |",
    ]


def test_table_bad_or_huge_spans_are_tolerated() -> None:
    html = "<table><tr><td colspan='x'>a</td><td colspan='0' rowspan='-3'>b</td></tr></table>"
    assert table_to_markdown(html) == "| a | b |\n| --- | --- |"
    huge = table_to_markdown("<table><tr><td colspan='99999999'>a</td></tr></table>")
    assert huge.splitlines()[0].count("a") == 1000


def test_table_pads_short_rows() -> None:
    html = "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>1</td></tr></table>"
    assert table_to_markdown(html).splitlines()[-1] == "| 1 |  |  |"


def test_table_pipes_and_newlines_in_cells() -> None:
    html = (
        "<table><tr><td>a|b</td><td>line1<br>line2</td></tr><tr><td><p>p1</p><p>p2</p></td><td>x\ny</td></tr></table>"
    )
    assert table_to_markdown(html).splitlines() == [
        "| a\\|b | line1 line2 |",
        "| --- | --- |",
        "| p1 p2 | x y |",
    ]


def test_table_nested_tags_and_math_in_cells() -> None:
    html = "<table><tr><td><b>bold</b> and <i>it</i><span> x</span></td><td><math>a|b</math></td></tr></table>"
    assert table_to_markdown(html).splitlines()[0] == "| bold and it x | $a\\|b$ |"


def test_table_nested_table_is_flattened_into_its_cell() -> None:
    html = (
        "<table><tr><td>outer<table><tr><td>in1</td><td>in2</td></tr></table>tail</td><td>b</td></tr>"
        "<tr><td>c</td><td>d</td></tr></table>"
    )
    assert table_to_markdown(html).splitlines() == [
        "| outer in1 in2 tail | b |",
        "| --- | --- |",
        "| c | d |",
    ]


def test_table_only_the_first_table_is_used() -> None:
    html = "text<table><tr><td>one</td></tr></table><table><tr><td>two</td></tr></table>"
    assert table_to_markdown(html) == "| one |\n| --- |"


@pytest.mark.parametrize("html", ["", "no table here", "<p>x</p>", "<table></table>", "<table><tr></tr></table>"])
def test_table_none_found(html: str) -> None:
    assert table_to_markdown(html) == ""


def test_table_malformed_missing_closers() -> None:
    html = "<table><tr><td>a<td>b<tr><td>c<td>d"
    assert table_to_markdown(html) == "| a | b |\n| --- | --- |\n| c | d |"


def test_table_cells_without_rows_and_stray_end_tags() -> None:
    assert table_to_markdown("<table><td>a</td><td>b</td></tr></td></table>") == "| a | b |\n| --- | --- |"
    assert table_to_markdown("</table></tr><table><tr><td>a</td></tr></table>") == "| a |\n| --- |"


def test_table_unclosed_table_and_script_in_cell() -> None:
    assert table_to_markdown("<table><tr><td>a<script>x()</script>b</td></tr>") == "| ab |\n| --- |"


def test_table_entities_and_empty_cells() -> None:
    html = "<table><tr><td>&lt;5 &amp; &gt;1</td><td></td></tr></table>"
    assert table_to_markdown(html) == "| <5 & >1 |  |\n| --- | --- |"


# --- split_list_items -------------------------------------------------------------------------


def test_list_flat() -> None:
    assert split_list_items("<ul><li>one</li><li> two\n words </li></ul>") == ["one", "two words"]
    assert split_list_items("<ol><li><b>bold</b> item</li></ol>") == ["bold item"]


def test_list_nested_items_are_own_entries_in_document_order() -> None:
    html = "<ul><li>a<ul><li>a1</li><li>a2</li></ul>tail</li><li>b</li></ul>"
    assert split_list_items(html) == ["a tail", "a1", "a2", "b"]


def test_list_parent_with_only_a_nested_list_is_dropped() -> None:
    assert split_list_items("<ul><li><ol><li>x</li></ol></li></ul>") == ["x"]


def test_list_unclosed_items() -> None:
    assert split_list_items("<ul><li>a<li>b<li>c</ul>") == ["a", "b", "c"]
    assert split_list_items("<ul><li>a<ul><li>b</ul><li>c</ul>") == ["a", "b", "c"]


def test_list_br_math_and_surrounding_text() -> None:
    html = "intro<ul><li>x<br>y</li><li><math>a_1</math></li></ul>outro"
    assert split_list_items(html) == ["x y", "$a_1$"]


@pytest.mark.parametrize("html", ["", "<p>no list</p>", "<ul></ul>", "<ul><li></li><li> </li></ul>"])
def test_list_none_found(html: str) -> None:
    assert split_list_items(html) == []


# --- first_heading_level ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "level"),
    [
        ("<h1>Title</h1>", 1),
        ("  \n<h3 class='x'>Sub</h3>", 3),
        ("<H6>Deep</H6>", 6),
        ("<h2>A</h2><h1>B</h1>", 2),
        ("<p>text</p><h1>x</h1>", None),
        ("text <h1>x</h1>", None),
        ("<h7>x</h7>", None),
        ("<h12>x</h12>", None),
        ("<hr>", None),
        ("<header>", None),
        ("", None),
    ],
)
def test_first_heading_level(html: str, level: int | None) -> None:
    assert first_heading_level(html) == level


# --- robustness -------------------------------------------------------------------------------


def test_helpers_never_raise_on_random_or_truncated_html() -> None:
    import random

    rng = random.Random(1234)
    pieces = [
        "<table>", "</table>", "<tr>", "</tr>", "<td colspan='2'>", "<td rowspan=\"3\">", "<th>", "</td>", "<ul>",
        "</ul>", "<ol>", "<li>", "</li>", "<math>", "</math>", "<pre>", "</pre>", "<br/>", "<p>", "</p>", "<h2>",
        "<script>", "</script>", "<", ">", "&", "&amp;", "&#x41;", "text", " ", "\n", "|", "<!--", "-->", "<a href=",
    ]  # fmt: skip
    for _ in range(400):
        html = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 40)))
        for cut in (len(html), len(html) // 2):
            fragment = html[:cut]
            assert isinstance(html_to_text(fragment), str)
            assert isinstance(table_to_markdown(fragment), str)
            assert all(isinstance(i, str) and i for i in split_list_items(fragment))
            first_heading_level(fragment)
