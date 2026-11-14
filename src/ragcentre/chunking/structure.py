"""``structure`` strategy (section 9).

Group consecutive blocks that share a heading path, up to ``max_chars``, never merging across a
heading boundary. A ``table`` or ``code`` block is kept whole; one larger than ``max_chars`` is
split by rows or lines, with the table's header rows repeated in each piece. A heading is
attached to the content that follows it, never emitted alone. With ``include_heading_path``
the chunk's ``context`` is its heading path joined by `` > ``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from ragcentre.chunking.base import BLOCK_SEPARATOR, ChunkDraft, StructureOptions, content_blocks
from ragcentre.chunking.recursive import recursive_spans
from ragcentre.models.ir import Block, DocumentIR

# Bump whenever a change alters this strategy's output for some input.
IMPL_VERSION = 1

# Below this many characters left for content after the headings, a heading is emitted alone
# rather than splitting content into fragments that small.
MIN_CONTENT_BUDGET = 50

_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


@dataclass
class Atom:
    text: str
    block_ids: list[str]


def split_lines(text: str, budget: int) -> list[str]:
    """Split on line boundaries into pieces of at most ``budget`` characters."""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if len(line) > budget:
            if current:
                pieces.append("\n".join(current))
                current, size = [], 0
            pieces.extend(line[s.start : s.end] for s in recursive_spans(line, 0, len(line), budget, 0))
            continue
        extra = len(line) + (1 if current else 0)
        if current and size + extra > budget:
            pieces.append("\n".join(current))
            current, size = [], 0
            extra = len(line)
        current.append(line)
        size += extra
    if current:
        pieces.append("\n".join(current))
    return [p for p in pieces if p.strip()]


def split_table(text: str, budget: int) -> list[str]:
    """Split a Markdown table by rows, repeating the header rows in every piece."""
    lines = text.split("\n")
    header_len = 2 if len(lines) >= 2 and _TABLE_RULE.match(lines[1]) else 1
    header = "\n".join(lines[:header_len])
    rows = lines[header_len:]
    capacity = budget - len(header) - 1
    if capacity < 1 or not rows:
        return split_lines(text, budget)
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for row in rows:
        if len(row) > capacity:
            if current:
                pieces.append(header + "\n" + "\n".join(current))
                current, size = [], 0
            pieces.extend(split_lines(row, budget))
            continue
        extra = len(row) + (1 if current else 0)
        if current and size + extra > capacity:
            pieces.append(header + "\n" + "\n".join(current))
            current, size = [], 0
            extra = len(row)
        current.append(row)
        size += extra
    if current:
        pieces.append(header + "\n" + "\n".join(current))
    return pieces


def split_block(block: Block, budget: int) -> list[str]:
    text = block.text
    if len(text) <= budget:
        return [text]
    if block.type == "table":
        return split_table(text, budget)
    if block.type == "code":
        return split_lines(text, budget)
    return [text[s.start : s.end] for s in recursive_spans(text, 0, len(text), budget, 0)]


def heading_groups(blocks: Sequence[Block]) -> list[list[Block]]:
    """Consecutive blocks with the same heading path; heading-only groups join the next group.

    Headings at the very end of a document have no content to attach to; they form their own
    group rather than being merged backwards across a heading boundary."""
    groups: list[list[Block]] = []
    for block in blocks:
        if groups and groups[-1][-1].heading_path == block.heading_path:
            groups[-1].append(block)
        else:
            groups.append([block])
    merged: list[list[Block]] = []
    pending: list[Block] = []
    for group in groups:
        if all(b.type == "heading" for b in group):
            pending.extend(group)
            continue
        merged.append(pending + group)
        pending = []
    if pending:
        merged.append(pending)
    return merged


def group_atoms(group: Sequence[Block], max_chars: int) -> list[Atom]:
    """Turn a group into atoms of at most ``max_chars``; leading headings join the first content."""
    headings: list[Block] = []
    i = 0
    while i < len(group) and group[i].type == "heading":
        headings.append(group[i])
        i += 1
    rest = group[i:]
    atoms: list[Atom] = []
    if headings and rest:
        prefix = BLOCK_SEPARATOR.join(h.text for h in headings)
        prefix_ids = [str(h.id) for h in headings]
        first = rest[0]
        budget = max_chars - len(prefix) - len(BLOCK_SEPARATOR)
        if budget >= MIN_CONTENT_BUDGET:
            pieces = split_block(first, budget)
            atoms.append(Atom(prefix + BLOCK_SEPARATOR + pieces[0], [*prefix_ids, str(first.id)]))
            atoms.extend(Atom(p, [str(first.id)]) for p in pieces[1:])
            rest = rest[1:]
        else:  # pathological: headings nearly fill a chunk on their own
            atoms.extend(Atom(p, prefix_ids) for p in split_lines(prefix, max_chars))
    elif headings:
        atoms.extend(Atom(p, [str(h.id) for h in headings]) for p in split_lines(
            BLOCK_SEPARATOR.join(h.text for h in headings), max_chars))  # fmt: skip
    for block in rest:
        atoms.extend(Atom(p, [str(block.id)]) for p in split_block(block, max_chars))
    return atoms


def pack(atoms: Sequence[Atom], max_chars: int, overlap_chars: int) -> list[Atom]:
    """Greedily pack atoms into chunks of at most ``max_chars``."""
    chunks: list[Atom] = []
    current: Atom | None = None
    for atom in atoms:
        if current is None:
            current = Atom(atom.text, list(atom.block_ids))
            continue
        if len(current.text) + len(BLOCK_SEPARATOR) + len(atom.text) <= max_chars:
            current.text += BLOCK_SEPARATOR + atom.text
            current.block_ids.extend(b for b in atom.block_ids if b not in current.block_ids)
            continue
        chunks.append(current)
        tail = current.text[-overlap_chars:].lstrip() if overlap_chars else ""
        if tail and len(tail) + len(BLOCK_SEPARATOR) + len(atom.text) <= max_chars:
            ids = [current.block_ids[-1]] + [b for b in atom.block_ids if b != current.block_ids[-1]]
            current = Atom(tail + BLOCK_SEPARATOR + atom.text, ids)
        else:
            current = Atom(atom.text, list(atom.block_ids))
    if current is not None:
        chunks.append(current)
    return chunks


def structure_drafts(doc: DocumentIR, options: StructureOptions) -> list[ChunkDraft]:
    by_id = {str(b.id): b for b in doc.blocks}
    drafts: list[ChunkDraft] = []
    for group in heading_groups(content_blocks(doc)):
        for chunk in pack(group_atoms(group, options.max_chars), options.max_chars, options.overlap_chars):
            context = None
            if options.include_heading_path and chunk.block_ids:
                path = by_id[chunk.block_ids[0]].heading_path
                context = " > ".join(path) if path else None
            drafts.append(ChunkDraft(text=chunk.text, block_ids=chunk.block_ids, context=context))
    return drafts


class StructureStrategy:
    name: ClassVar[str] = "structure"
    impl_version: ClassVar[int] = IMPL_VERSION
    options_model: ClassVar[type[StructureOptions]] = StructureOptions
    needs_provider: ClassVar[bool] = False

    def chunk(self, doc: DocumentIR, options: Any, provider: Any = None) -> list[ChunkDraft]:
        assert isinstance(options, StructureOptions)
        return structure_drafts(doc, options)
