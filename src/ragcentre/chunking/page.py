"""``page`` strategy: one chunk per page; a page longer than ``max_chars`` is split recursively.
Falls back to ``structure`` (with the same ``max_chars``) when the IR has no page numbers."""

from __future__ import annotations

from typing import Any, ClassVar

from ragcentre.chunking.base import ChunkDraft, PageOptions, StructureOptions, blocks_in, content_blocks, join_blocks
from ragcentre.chunking.recursive import recursive_spans
from ragcentre.chunking.structure import structure_drafts
from ragcentre.models.ir import Block, DocumentIR

# Bump whenever a change alters this strategy's output for some input (including the fallback).
IMPL_VERSION = 1


class PageStrategy:
    name: ClassVar[str] = "page"
    impl_version: ClassVar[int] = IMPL_VERSION
    options_model: ClassVar[type[PageOptions]] = PageOptions
    needs_provider: ClassVar[bool] = False

    def chunk(self, doc: DocumentIR, options: Any, provider: Any = None) -> list[ChunkDraft]:
        assert isinstance(options, PageOptions)
        blocks = content_blocks(doc)
        if not any(b.page is not None for b in blocks):
            return structure_drafts(doc, StructureOptions(max_chars=options.max_chars))
        pages: list[tuple[int, list[Block]]] = []
        current_page = next(b.page for b in blocks if b.page is not None)
        for block in blocks:
            # A block without a page number belongs to the page before it.
            page = block.page if block.page is not None else current_page
            current_page = page
            if pages and pages[-1][0] == page:
                pages[-1][1].append(block)
            else:
                pages.append((page, [block]))
        drafts: list[ChunkDraft] = []
        for _, page_blocks in pages:
            text, spans = join_blocks(page_blocks)
            for s in recursive_spans(text, 0, len(text), options.max_chars, 0):
                drafts.append(ChunkDraft(text=text[s.start : s.end], block_ids=blocks_in(spans, s.start, s.end)))
        return drafts
