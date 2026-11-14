from __future__ import annotations

import itertools
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ragcentre.chunking import embedded_text, get_strategy, options_with_defaults, run_strategy, strategies
from ragcentre.chunking.base import Chunk
from ragcentre.errors import ExecutionError
from ragcentre.hashing import chunk_id
from ragcentre.models.ir import DocumentIR, normalise_ir

FIXTURES = Path(__file__).parent / "fixtures" / "ir"
GOLDEN = Path(__file__).parent / "golden" / "chunking"
IRS = ["prose", "headed_table", "paged"]
BUILTIN = ["fixed", "recursive", "structure", "page"]
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


def load(name: str) -> DocumentIR:
    return normalise_ir(json.loads((FIXTURES / f"{name}.json").read_text()))


def chunks_json(chunks: list[Chunk]) -> list[dict[str, Any]]:
    return [c.to_json() for c in chunks]


@pytest.mark.parametrize("ir_name", IRS)
@pytest.mark.parametrize("strategy", BUILTIN)
def test_golden(strategy: str, ir_name: str) -> None:
    got = chunks_json(run_strategy(strategy, load(ir_name), {}))
    path = GOLDEN / f"{strategy}__{ir_name}.json"
    if UPDATE or not path.exists():
        if not UPDATE:
            pytest.fail(f"missing golden file {path.name}; run with UPDATE_GOLDEN=1")
        path.write_text(json.dumps(got, indent=1, ensure_ascii=False) + "\n")
    assert got == json.loads(path.read_text())


def _longest_token(text: str) -> int:
    return max((len(t) for t in re.split(r"\s+", text)), default=0)


@pytest.mark.parametrize("ir_name", IRS)
@pytest.mark.parametrize("strategy", BUILTIN)
@pytest.mark.parametrize("max_chars", [100, 300, 1200])
def test_no_chunk_exceeds_max_chars(strategy: str, ir_name: str, max_chars: int) -> None:
    opts: dict[str, Any] = {"max_chars": max_chars}
    if strategy in ("fixed", "recursive"):
        opts["overlap_chars"] = max_chars // 5
    chunks = run_strategy(strategy, load(ir_name), opts)
    assert chunks
    for c in chunks:
        assert len(c.text) <= max_chars or len(c.text.split()) == 1, (c.ordinal, len(c.text))
        assert c.text == c.text.strip() and c.text
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


@pytest.mark.parametrize("strategy", BUILTIN)
def test_headers_and_footers_dropped(strategy: str) -> None:
    for ir_name in IRS:
        doc = load(ir_name)
        dropped = {b.id for b in doc.blocks if b.type in ("page_header", "page_footer")}
        for c in run_strategy(strategy, doc, {}):
            assert not dropped & set(c.block_ids)
            assert "ACME Air 300 — Service manual" not in c.text


def test_oversized_table_is_split_with_header_repeated() -> None:
    doc = load("headed_table")
    table = next(b for b in doc.blocks if b.type == "table")
    assert len(table.text) > 1200
    header = "| Code | Meaning | Action |\n|---|---|---|"
    parts = [c for c in run_strategy("structure", doc, {}) if table.id in c.block_ids]
    assert len(parts) >= 2
    for part in parts:
        assert header in part.text
        assert len(part.text) <= 1200
    rows = [ln for p in parts for ln in p.text.split("\n") if re.match(r"^\| E\d+", ln)]
    assert rows == [ln for ln in table.text.split("\n") if re.match(r"^\| E\d+", ln)]  # every row exactly once


def test_structure_rules() -> None:
    doc = load("headed_table")
    chunks = run_strategy("structure", doc, {})
    by_id = {b.id: b for b in doc.blocks}
    small_table = next(b for b in doc.blocks if b.type == "table" and len(b.text) < 100)
    code = next(b for b in doc.blocks if b.type == "code")
    for c in chunks:
        blocks = [by_id[i] for i in c.block_ids]
        # Never merge across a heading boundary: one heading path per chunk, apart from leading
        # headings attached to the content that follows them.
        content = [b for b in blocks if b.type != "heading"]
        assert len({tuple(b.heading_path) for b in content}) <= 1
        # A heading is never emitted alone; the one exception is a document that ends in a
        # bare heading, which has no following content to attach to.
        if all(b.type == "heading" for b in blocks):
            assert c.ordinal == len(chunks) - 1 and c.text == "Appendix"
        assert "Appendix" not in c.text or c.ordinal == len(chunks) - 1
        assert c.context == (" > ".join(c.heading_path) or None)
    # A small table is kept whole.
    assert any(small_table.text in c.text for c in chunks)
    # The code block (> max_chars) is split on line boundaries.
    code_parts = [c for c in chunks if code.id in c.block_ids]
    assert len(code_parts) >= 2
    code_lines = set(code.text.split("\n"))
    for part in code_parts:
        assert all(line in code_lines for line in part.text.split("\n") if line.startswith("set "))


def test_structure_without_heading_path() -> None:
    chunks = run_strategy("structure", load("headed_table"), {"include_heading_path": False})
    assert all(c.context is None for c in chunks)
    assert chunks[0].heading_path == ["Service manual"]


def test_page_strategy_one_chunk_per_page_and_fallback() -> None:
    doc = load("paged")
    chunks = run_strategy("page", doc, {})
    pages = [(c.page_start, c.page_end) for c in chunks]
    assert all(a == b for a, b in pages)
    assert [p for p, _ in pages].count(1) == 1 and [p for p, _ in pages].count(2) == 1
    assert [p for p, _ in pages].count(3) >= 2  # page 3 is longer than max_chars
    caption = next(b for b in doc.blocks if b.type == "caption")
    assert any(caption.id in c.block_ids and c.page_start == 2 for c in chunks)
    # No page numbers: same output as structure with the same max_chars.
    prose = load("headed_table")
    assert chunks_json(run_strategy("page", prose, {"max_chars": 1500})) == chunks_json(
        run_strategy("structure", prose, {"max_chars": 1500})
    )


def test_fixed_overlap() -> None:
    chunks = run_strategy("fixed", load("prose"), {"max_chars": 200, "overlap_chars": 50})
    raw = run_strategy("fixed", load("prose"), {"max_chars": 200, "overlap_chars": 0})
    assert len(chunks) > len(raw)


def test_recursive_spans_tile_text_with_overlap() -> None:
    from ragcentre.chunking.base import content_blocks, join_blocks
    from ragcentre.chunking.recursive import recursive_spans

    text, _ = join_blocks(content_blocks(load("prose")))
    spans = recursive_spans(text, 0, len(text), 300, 60)
    assert spans[0].start == 0 and spans[-1].end == len(text)
    for prev, nxt in itertools.pairwise(spans):
        assert nxt.start <= prev.end  # contiguous: nothing is skipped
        assert prev.end - nxt.start <= 60  # at most overlap_chars carried over
        assert nxt.start > prev.start
        assert nxt.end - nxt.start <= 300
    assert any(prev.end > nxt.start for prev, nxt in itertools.pairwise(spans))


def test_chunk_ids_identical_across_runs() -> None:
    for strategy in BUILTIN:
        for ir_name in IRS:
            a = run_strategy(strategy, load(ir_name), {})
            b = run_strategy(strategy, load(ir_name), {})
            assert [chunk_id("f" * 64, "doc", c.ordinal) for c in a] == [
                chunk_id("f" * 64, "doc", c.ordinal) for c in b
            ]
            assert chunks_json(a) == chunks_json(b)


@pytest.mark.parametrize("strategy", BUILTIN)
def test_empty_document_yields_zero_chunks(strategy: str) -> None:
    empty = normalise_ir({"schema_version": "1.0", "blocks": []})
    assert run_strategy(strategy, empty, {}) == []
    only_noise = normalise_ir(
        {
            "schema_version": "1.0",
            "blocks": [
                {"type": "page_header", "text": "header", "page": 1},
                {"type": "figure", "text": "", "page": 1},
                {"type": "paragraph", "text": "   \n  ", "page": 1},
            ],
        }
    )
    assert run_strategy(strategy, only_noise, {}) == []


def test_option_validation_and_defaults() -> None:
    assert options_with_defaults(strategies()["fixed"], {}) == {"max_chars": 1000, "overlap_chars": 100}
    assert options_with_defaults(strategies()["structure"], {"max_chars": 500}) == {
        "max_chars": 500,
        "overlap_chars": 0,
        "include_heading_path": True,
    }
    assert options_with_defaults(strategies()["page"], {}) == {"max_chars": 4000}
    for strategy, opts in [
        ("fixed", {"max_chars": 99}),
        ("fixed", {"max_chars": 20001}),
        ("recursive", {"max_chars": 1000, "overlap_chars": 500}),
        ("structure", {"overlap_chars": 600}),
        ("page", {"overlap_chars": 10}),
        ("fixed", {"bogus": 1}),
    ]:
        with pytest.raises(ValidationError):
            options_with_defaults(strategies()[strategy], opts)
    assert options_with_defaults(strategies()["http"], {"anything": [1]}) == {"anything": [1]}


def test_embedded_text() -> None:
    assert embedded_text("body", None) == "body"
    assert embedded_text("body", "Guide > Setup") == "Guide > Setup\n\nbody"
    chunk = run_strategy("structure", load("headed_table"), {})[0]
    assert chunk.embedded == f"{chunk.context}\n\n{chunk.text}"


def test_impl_versions_and_registry() -> None:
    assert set(strategies()) >= {"fixed", "recursive", "structure", "page", "http"}
    for name in [*BUILTIN, "http"]:
        s = get_strategy(name)
        assert s is not None and isinstance(s.impl_version, int)


class FakeChunker:
    name = "fake"

    def __init__(self, response: list[dict[str, Any]]) -> None:
        self.response = response
        self.received: dict[str, Any] = {}

    def chunk(self, document: dict[str, Any], options: dict[str, Any]) -> list[dict[str, Any]]:
        self.received = {"document": document, "options": options}
        return self.response


def test_http_strategy_fills_in_pages_and_heading_path() -> None:
    doc = load("paged")
    ids = [b.id for b in doc.blocks if b.type == "paragraph"]
    provider = FakeChunker([
        {"text": "first", "context": "ctx", "block_ids": [ids[0], ids[1]], "metadata": {"k": 1}},
        {"text": "  ", "block_ids": [ids[2]]},
        {"text": "later", "block_ids": [ids[-1]]},
    ])  # fmt: skip
    chunks = run_strategy("http", doc, {"mode": "x"}, provider)
    assert provider.received["options"] == {"mode": "x"}
    assert [c.text for c in chunks] == ["first", "later"]
    assert chunks[0].page_start == 1 and chunks[0].context == "ctx" and chunks[0].metadata == {"k": 1}
    assert chunks[0].heading_path == ["Quarterly report"]
    assert chunks[1].page_start == 4 and chunks[1].ordinal == 1
    with pytest.raises(ExecutionError):
        run_strategy("http", doc, {}, FakeChunker([{"text": "x", "block_ids": ["nope"]}]))
    with pytest.raises(ExecutionError):
        run_strategy("http", doc, {}, FakeChunker([{"text": 3, "block_ids": []}]))
