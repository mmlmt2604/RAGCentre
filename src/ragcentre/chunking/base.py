"""Chunk model, the embedded-string function, option models and shared helpers (section 9)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ragcentre.models.ir import Block, DocumentIR

DROPPED_TYPES = frozenset({"page_header", "page_footer"})
BLOCK_SEPARATOR = "\n\n"


def embedded_text(text: str, context: str | None) -> str:
    """The string that is embedded and keyword-indexed for a chunk. Used everywhere."""
    return f"{context}\n\n{text}" if context else text


@dataclass
class ChunkDraft:
    """What a strategy produces; ``finalize`` fills in ordinal, pages and heading path."""

    text: str
    block_ids: list[str]
    context: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    context: str | None
    block_ids: list[str]
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    metadata: dict[str, Any]

    @property
    def embedded(self) -> str:
        return embedded_text(self.text, self.context)

    def to_json(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "text": self.text,
            "context": self.context,
            "block_ids": self.block_ids,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "heading_path": self.heading_path,
            "metadata": self.metadata,
        }


# ---- options -----------------------------------------------------------------------------------


class ChunkOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WindowOptions(ChunkOptions):
    max_chars: int = Field(default=1000, ge=100, le=20000)
    overlap_chars: int = Field(default=100, ge=0)

    @model_validator(mode="after")
    def _overlap(self) -> WindowOptions:
        if self.overlap_chars >= self.max_chars / 2:
            raise ValueError("overlap_chars must be less than max_chars / 2")
        return self


class StructureOptions(WindowOptions):
    max_chars: int = Field(default=1200, ge=100, le=20000)
    overlap_chars: int = Field(default=0, ge=0)
    include_heading_path: bool = True


class PageOptions(ChunkOptions):
    max_chars: int = Field(default=4000, ge=100, le=20000)


class HttpOptions(BaseModel):
    """Options for the ``http`` strategy are passed through to the service unchanged."""

    model_config = ConfigDict(extra="allow")


# ---- strategy protocol ---------------------------------------------------------------------------


class ChunkStrategy(Protocol):
    name: ClassVar[str]
    impl_version: ClassVar[int]
    options_model: ClassVar[type[BaseModel]]
    needs_provider: ClassVar[bool]

    def chunk(self, doc: DocumentIR, options: Any, provider: Any = None) -> list[ChunkDraft]: ...


# ---- helpers ---------------------------------------------------------------------------------------


def content_blocks(doc: DocumentIR) -> list[Block]:
    """Blocks in reading order, without page headers and footers."""
    return [b for b in doc.blocks if b.type not in DROPPED_TYPES]


@dataclass(frozen=True)
class Span:
    start: int
    end: int


def join_blocks(blocks: Sequence[Block]) -> tuple[str, list[tuple[Span, str]]]:
    """Join block texts with blank lines; return the text and each block's span within it."""
    parts: list[str] = []
    spans: list[tuple[Span, str]] = []
    pos = 0
    for i, block in enumerate(blocks):
        if i:
            parts.append(BLOCK_SEPARATOR)
            pos += len(BLOCK_SEPARATOR)
        parts.append(block.text)
        spans.append((Span(pos, pos + len(block.text)), str(block.id)))
        pos += len(block.text)
    return "".join(parts), spans


def blocks_in(spans: Sequence[tuple[Span, str]], start: int, end: int) -> list[str]:
    return [bid for span, bid in spans if span.start < end and span.end > start]


def _block_pages(block: Block) -> list[int]:
    """The pages a block covers: its ``page``, and ``metadata.page_end`` when the parser says it
    runs on (a table split over a page break). A malformed ``page_end`` is ignored."""
    if block.page is None:
        return []
    end = block.metadata.get("page_end")
    if isinstance(end, int) and not isinstance(end, bool) and end > block.page:
        return [block.page, end]
    return [block.page]


def finalize(doc: DocumentIR, drafts: Sequence[ChunkDraft]) -> list[Chunk]:
    """Drop empty chunks, assign ordinals and fill pages and heading path from the blocks."""
    by_id = {str(b.id): b for b in doc.blocks}
    out: list[Chunk] = []
    for draft in drafts:
        text = draft.text.strip()
        if not text:
            continue
        blocks = [by_id[b] for b in draft.block_ids if b in by_id]
        pages = [page for b in blocks for page in _block_pages(b)]
        context = draft.context.strip() if draft.context else None
        out.append(
            Chunk(
                ordinal=len(out),
                text=text,
                context=context or None,
                block_ids=list(dict.fromkeys(draft.block_ids)),
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
                heading_path=list(blocks[0].heading_path) if blocks else [],
                metadata=dict(draft.metadata),
            )
        )
    return out


StrategyFactory = Callable[[], ChunkStrategy]


def validate_options(strategy: ChunkStrategy, options: Mapping[str, Any] | None) -> BaseModel:
    """Validate options and fill defaults. Raises ``pydantic.ValidationError``."""
    return strategy.options_model.model_validate(dict(options or {}))


def options_with_defaults(strategy: ChunkStrategy, options: Mapping[str, Any] | None) -> dict[str, Any]:
    model = validate_options(strategy, options)
    return model.model_dump(mode="json")
