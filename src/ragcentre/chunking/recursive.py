"""``recursive`` strategy: split on the first separator that applies, recurse into oversized
pieces, merge adjacent pieces up to ``max_chars`` and carry ``overlap_chars`` of trailing text
into the next chunk.

Everything works on character spans of the joined document text, so a chunk's text is always a
contiguous slice and its block ids follow directly from the block spans.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ragcentre.chunking.base import ChunkDraft, Span, WindowOptions, blocks_in, content_blocks, join_blocks
from ragcentre.models.ir import DocumentIR

# Bump whenever a change alters this strategy's output for some input.
IMPL_VERSION = 1

SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ". ", " ", "")


def split_spans(
    text: str, start: int, end: int, max_chars: int, separators: tuple[str, ...] = SEPARATORS
) -> list[Span]:
    """Split ``text[start:end]`` into contiguous spans of at most ``max_chars`` characters.

    Separators stay attached to the end of the piece before them, so the spans tile the range.
    """
    if end - start <= max_chars:
        return [Span(start, end)] if end > start else []
    segment = text[start:end]
    for i, sep in enumerate(separators):
        if sep == "":
            return [Span(p, min(p + max_chars, end)) for p in range(start, end, max_chars)]
        if sep not in segment:
            continue
        spans: list[Span] = []
        pos = start
        while pos < end:
            found = text.find(sep, pos, end)
            piece_end = end if found == -1 else found + len(sep)
            if piece_end - pos <= max_chars:
                spans.append(Span(pos, piece_end))
            else:
                spans.extend(split_spans(text, pos, piece_end, max_chars, separators[i + 1 :]))
            pos = piece_end
        return spans
    return [Span(p, min(p + max_chars, end)) for p in range(start, end, max_chars)]  # pragma: no cover


def _word_start(text: str, start: int, limit: int) -> int:
    """Move ``start`` forward to the beginning of a word, without passing ``limit``."""
    if start <= 0 or start >= limit or text[start - 1].isspace():
        return start
    for i in range(start, limit):
        if text[i].isspace():
            j = i
            while j < limit and text[j].isspace():
                j += 1
            return j
    return limit


def merge_spans(text: str, pieces: list[Span], max_chars: int, overlap_chars: int) -> list[Span]:
    """Greedily merge contiguous pieces up to ``max_chars``; each new chunk starts with up to
    ``overlap_chars`` of the previous chunk's trailing text, beginning at a word boundary."""
    chunks: list[Span] = []
    cur: Span | None = None
    for piece in pieces:
        if cur is None:
            cur = piece
            continue
        if piece.end - cur.start <= max_chars:
            cur = Span(cur.start, piece.end)
            continue
        chunks.append(cur)
        new_start = min(max(piece.start - overlap_chars, piece.end - max_chars, cur.start + 1), piece.start)
        cur = Span(_word_start(text, new_start, piece.start), piece.end)
    if cur is not None:
        chunks.append(cur)
    return chunks


def recursive_spans(text: str, start: int, end: int, max_chars: int, overlap_chars: int) -> list[Span]:
    return merge_spans(text, split_spans(text, start, end, max_chars), max_chars, overlap_chars)


class RecursiveStrategy:
    name: ClassVar[str] = "recursive"
    impl_version: ClassVar[int] = IMPL_VERSION
    options_model: ClassVar[type[WindowOptions]] = WindowOptions
    needs_provider: ClassVar[bool] = False

    def chunk(self, doc: DocumentIR, options: Any, provider: Any = None) -> list[ChunkDraft]:
        assert isinstance(options, WindowOptions)
        text, spans = join_blocks(content_blocks(doc))
        return [
            ChunkDraft(text=text[s.start : s.end], block_ids=blocks_in(spans, s.start, s.end))
            for s in recursive_spans(text, 0, len(text), options.max_chars, options.overlap_chars)
        ]
