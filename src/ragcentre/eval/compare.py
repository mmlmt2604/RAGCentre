"""Paired bootstrap comparison of two pipelines (section 14.6). Pure Python, fixed seed."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

RESAMPLES = 2000
SEED = 1729


@dataclass(frozen=True)
class Comparison:
    mean_a: float
    mean_b: float
    diff: float  # mean_b - mean_a
    ci_low: float
    ci_high: float
    significant: bool


def paired_bootstrap(
    a: Sequence[float], b: Sequence[float], resamples: int = RESAMPLES, seed: int = SEED
) -> Comparison:
    if len(a) != len(b):
        raise ValueError("paired samples must have the same length")
    n = len(a)
    if n == 0:
        return Comparison(0.0, 0.0, 0.0, 0.0, 0.0, False)
    diffs = [y - x for x, y in zip(a, b, strict=True)]
    rng = random.Random(seed)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    low = means[int(0.025 * (resamples - 1))]
    high = means[round(0.975 * (resamples - 1))]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    return Comparison(mean_a, mean_b, mean_b - mean_a, low, high, low > 0 or high < 0)
