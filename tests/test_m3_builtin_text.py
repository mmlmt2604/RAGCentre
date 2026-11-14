from __future__ import annotations

from ragcentre.models.ir import normalise_ir
from ragcentre.providers.parsers.builtin_text import parse_text

MD = """\ufeff# Guide

Intro paragraph
continues here.

## Setup ##

- first item
- second item
  continued
* star item
1. numbered

```python
def f():

    return 1
```

Closing words.
#not a heading
"""


def test_markdown_blocks() -> None:
    ir = parse_text(MD.encode(), "text/markdown")
    got = [(b["type"], b["text"]) for b in ir["blocks"]]
    assert got == [
        ("heading", "Guide"),
        ("paragraph", "Intro paragraph\ncontinues here."),
        ("heading", "Setup"),
        ("list_item", "first item"),
        ("list_item", "second item\ncontinued"),
        ("list_item", "star item"),
        ("list_item", "numbered"),
        ("code", "def f():\n\n    return 1"),
        ("paragraph", "Closing words.\n#not a heading"),
    ]
    assert ir["blocks"][0]["level"] == 1 and ir["blocks"][2]["level"] == 2
    assert ir["blocks"][7]["metadata"] == {"language": "python"}
    doc = normalise_ir(ir)
    assert doc.blocks[3].heading_path == ["Guide", "Setup"]


def test_plain_text_is_paragraphs_only() -> None:
    ir = parse_text(b"# not a heading\r\n\r\n- not a list\n\n\n\nlast", "text/plain")
    assert [(b["type"], b["text"]) for b in ir["blocks"]] == [
        ("paragraph", "# not a heading"),
        ("paragraph", "- not a list"),
        ("paragraph", "last"),
    ]


def test_unclosed_fence_and_empty() -> None:
    ir = parse_text(b"```\ncode", "text/markdown")
    assert ir["blocks"] == [{"type": "code", "text": "code"}]
    assert parse_text(b"", "text/markdown")["blocks"] == []
    assert parse_text(b"\n\n   \n", "text/plain")["blocks"] == []
