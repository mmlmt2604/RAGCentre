"""Shape serialized passages without changing retrieval, caches or evaluation inputs."""

from __future__ import annotations

from ragcentre.models.api import QueryResponse, QueryResult


def passage_chars(hit: QueryResult) -> int:
    """Count repeated transmitted passage content in Unicode codepoints, not JSON bytes."""
    return sum(
        len(s) for s in (hit.text, hit.context or "", hit.enrichment or "", *hit.context_before, *hit.context_after)
    )


def budget_response(response: QueryResponse, max_chars: int | None) -> QueryResponse:
    """Remove supplemental content before dropping low-ranked hits; clip only an oversized first hit.

    Work on a copy: neither a cached response nor retrieval's original passages may be truncated.
    IDs, scores, rank, ordinals, source matches and transform/debug information stay intact.
    """
    if max_chars is None:
        return response
    total = sum(passage_chars(hit) for hit in response.results)
    if total <= max_chars:
        return response
    shaped = response.model_copy(deep=True)
    shaped.truncated = True
    for hit in reversed(shaped.results):
        for neighbours in (hit.context_after, hit.context_before):
            while neighbours and total > max_chars:
                total -= len(neighbours.pop() if neighbours is hit.context_after else neighbours.pop(0))
        for field in ("context", "enrichment"):
            content = getattr(hit, field)
            if total > max_chars and content is not None:
                total -= len(content)
                setattr(hit, field, None)
    while len(shaped.results) > 1 and total > max_chars:
        total -= passage_chars(shaped.results.pop())
    if shaped.results and total > max_chars:
        # All supplemental content is gone by this point. No marker consumes extra budget.
        shaped.results[0].text = shaped.results[0].text[:max_chars]
    return shaped
