"""Retrieval metrics (section 14.4). Pure Python."""

from __future__ import annotations

import math
from collections.abc import Sequence

DEFAULT_K_VALUES = (1, 3, 5, 10)


def metric_names(k_values: Sequence[int]) -> list[str]:
    names = [f"{m}@{k}" for k in k_values for m in ("hit_rate", "recall", "precision", "ndcg")]
    return [*names, "mrr"]


def item_metrics(hits: Sequence[Sequence[int]], n_targets: int, k_values: Sequence[int]) -> dict[str, float]:
    """``hits[i]`` is the list of target indexes matched by the result at rank i + 1."""
    out: dict[str, float] = {}
    for k in k_values:
        top = hits[:k]
        found: set[int] = set()
        dcg = 0.0
        for i, matched in enumerate(top, start=1):
            first = set(matched) - found  # a target counts once, at the rank it is first matched
            dcg += len(first) / math.log2(i + 1)
            found |= first
        idcg = sum(1.0 / math.log2(i + 1) for i in range(1, min(n_targets, k) + 1))
        out[f"hit_rate@{k}"] = 1.0 if found else 0.0
        out[f"recall@{k}"] = len(found) / n_targets if n_targets else 0.0
        out[f"precision@{k}"] = sum(1 for m in top if m) / k
        out[f"ndcg@{k}"] = dcg / idcg if idcg else 0.0
    limit = max(k_values)
    out["mrr"] = next((1.0 / i for i, m in enumerate(hits[:limit], start=1) if m), 0.0)
    return out


def zero_metrics(k_values: Sequence[int]) -> dict[str, float]:
    return dict.fromkeys(metric_names(k_values), 0.0)


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile; 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]
