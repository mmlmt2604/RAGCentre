"""Built-in prompts for query transforms and their implementation versions.

Bump a mode's version whenever a change to its prompt or output handling alters what the same
query produces. The version enters the pipeline fingerprint, so evaluation results never mix
two behaviours.
"""

from __future__ import annotations

TRANSFORM_MODES: tuple[str, ...] = ("rewrite", "multi_query", "hyde", "decompose")

TRANSFORM_IMPL: dict[str, int] = {"rewrite": 1, "multi_query": 1, "hyde": 1, "decompose": 1}
