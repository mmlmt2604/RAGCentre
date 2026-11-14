"""``fixed`` strategy: join block texts with blank lines and slide a window of ``max_chars``
with ``overlap_chars`` overlap."""

from __future__ import annotations

from typing import Any, ClassVar

from ragcentre.chunking.base import ChunkDraft, WindowOptions, blocks_in, content_blocks, join_blocks
from ragcentre.models.ir import DocumentIR

# Bump whenever a change alters this strategy's output for some input.
IMPL_VERSION = 1


class FixedStrategy:
    name: ClassVar[str] = "fixed"
    impl_version: ClassVar[int] = IMPL_VERSION
    options_model: ClassVar[type[WindowOptions]] = WindowOptions
    needs_provider: ClassVar[bool] = False

    def chunk(self, doc: DocumentIR, options: Any, provider: Any = None) -> list[ChunkDraft]:
        assert isinstance(options, WindowOptions)
        text, spans = join_blocks(content_blocks(doc))
        step = options.max_chars - options.overlap_chars
        drafts: list[ChunkDraft] = []
        start = 0
        while start < len(text):
            end = min(start + options.max_chars, len(text))
            drafts.append(ChunkDraft(text=text[start:end], block_ids=blocks_in(spans, start, end)))
            if end == len(text):
                break
            start += step
        return drafts
