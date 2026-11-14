"""Reranker client for Cohere/Jina-style ``POST /rerank`` servers (section 8.3).

Request ``{"model", "query", "documents": [...], "top_n"}``; response
``{"results": [{"index", "relevance_score"}]}``. Results are returned best first.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from pydantic import Field

from ragcentre.providers.base import BuildEnv, HttpConfig, HttpService, ProviderTypeSpec, invalid_response


class HttpRerankerConfig(HttpConfig):
    model: str = Field(min_length=1, description="Model name")
    revision: str = Field(default="", description="Optional revision tag; change it to force a new fingerprint")


class HttpReranker(HttpService):
    def __init__(self, config: HttpRerankerConfig, secrets: dict[str, str], env: BuildEnv) -> None:
        super().__init__(config, secrets, env)
        self.model = config.model

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        if not documents:
            return []
        body = self.json(
            "POST",
            "/rerank",
            json={"model": self.model, "query": query, "documents": list(documents), "top_n": top_n},
        )
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise invalid_response(self.name, "missing 'results' list")
        out: list[tuple[int, float]] = []
        seen: set[int] = set()
        for item in results:
            if not isinstance(item, dict):
                raise invalid_response(self.name, "each result must be an object")
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if not isinstance(index, int) or not 0 <= index < len(documents) or index in seen:
                raise invalid_response(self.name, f"bad or duplicate result index {index!r}")
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
                raise invalid_response(self.name, "relevance_score must be a finite number")
            seen.add(index)
            out.append((index, float(score)))
        out.sort(key=lambda x: (-x[1], x[0]))
        return out


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> HttpReranker:
    assert isinstance(config, HttpRerankerConfig)
    return HttpReranker(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="reranker",
    type="http",
    label="Reranker (HTTP)",
    description="Cohere/Jina-style POST /rerank.",
    config_model=HttpRerankerConfig,
    fingerprint_fields=("model", "revision"),
    secret_keys=("api_key",),
    factory=_factory,
)
