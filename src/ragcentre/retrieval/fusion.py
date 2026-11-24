"""Fusion methods (section 13): rank-based RRF and min-max weighted fusion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceMatch:
    source: str
    chunk_id: str
    score: float
    rank: int  # 1-based within the source


def rrf(
    source_matches: Mapping[str, Sequence[SourceMatch]],
    weights: Mapping[str, float],
    k: int = 60,
) -> list[tuple[str, float]]:
    """RRF with ranks starting at 1; sources where a chunk is absent contribute 0."""
    totals: dict[str, float] = {}
    for source, matches in source_matches.items():
        weight = float(weights.get(source, 1.0))
        for match in matches:
            totals[match.chunk_id] = totals.get(match.chunk_id, 0.0) + weight / (k + match.rank)
    return sorted(totals.items(), key=lambda x: (-x[1], x[0]))


def weighted(
    source_matches: Mapping[str, Sequence[SourceMatch]],
    weights: Mapping[str, float],
) -> list[tuple[str, float]]:
    """Min-max normalise each source's scores to [0, 1], then weighted sum.

    A source with one result or equal scores gives every result 1.0. A source with no results
    contributes nothing.
    """
    normalised: dict[str, dict[str, float]] = {}
    for source, matches in source_matches.items():
        weight = float(weights.get(source, 1.0))
        if not matches:
            continue
        scores = [m.score for m in matches]
        low, high = min(scores), max(scores)
        scale = (high - low) or 1.0
        normalised[source] = {m.chunk_id: weight * ((m.score - low) / scale if high > low else 1.0) for m in matches}
    totals: dict[str, float] = {}
    for per_source in normalised.values():
        for chunk_id, value in per_source.items():
            totals[chunk_id] = totals.get(chunk_id, 0.0) + value
    return sorted(totals.items(), key=lambda x: (-x[1], x[0]))


def interleave(groups: Sequence[Sequence[tuple[str, float]]], limit: int) -> list[tuple[int, str, float]]:
    """Round-robin merge of ranked lists: one chunk from each group in turn.

    Chunks already taken are skipped, and a group that runs out drops out of the rotation. Returns
    ``(group index, chunk id, score)`` for up to ``limit`` chunks. No group can take more than its
    share of the slots while the others still have results.
    """
    positions = [0] * len(groups)
    taken: set[str] = set()
    out: list[tuple[int, str, float]] = []
    while len(out) < limit:
        progressed = False
        for g, ranked in enumerate(groups):
            while positions[g] < len(ranked) and ranked[positions[g]][0] in taken:
                positions[g] += 1
            if positions[g] == len(ranked):
                continue
            chunk_id, score = ranked[positions[g]]
            positions[g] += 1
            taken.add(chunk_id)
            out.append((g, chunk_id, score))
            progressed = True
            if len(out) == limit:
                break
        if not progressed:
            break
    return out
