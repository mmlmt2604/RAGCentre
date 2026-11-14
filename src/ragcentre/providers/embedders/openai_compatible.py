"""Embedder for any server exposing an OpenAI-compatible ``POST /embeddings`` (section 8.2).

Request: ``{"model", "input": [...], "encoding_format": "float"}`` plus ``"dimensions"`` only when
configured. The response's ``data[*].embedding`` is ordered by ``data[*].index``; the count must
equal the input count and all vectors must share one length.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from pydantic import Field

from ragcentre.providers.base import BuildEnv, HttpConfig, HttpService, ProviderTypeSpec, Role, invalid_response
from ragcentre.vectors import l2_normalise


class OpenAICompatibleConfig(HttpConfig):
    model: str = Field(min_length=1, description="Model name")
    dimensions: int | None = Field(default=None, ge=1, le=65536, description="Output dimensions (optional)")
    normalize: bool = Field(default=False, description="L2-normalise vectors in RAGCentre")
    query_prefix: str = Field(default="", description="Prepended to queries before embedding")
    document_prefix: str = Field(default="", description="Prepended to documents before embedding")
    batch_size: int = Field(default=64, ge=1, le=4096, description="Texts per request")
    max_input_chars: int | None = Field(default=None, ge=1, description="Longer inputs are truncated")
    revision: str = Field(default="", description="Optional revision tag; change it to force a new fingerprint")


class OpenAICompatibleEmbedder(HttpService):
    def __init__(self, config: OpenAICompatibleConfig, secrets: dict[str, str], env: BuildEnv) -> None:
        super().__init__(config, secrets, env)
        self.config = config
        self.batch_size = config.batch_size
        self.max_input_chars = config.max_input_chars

    def prepare(self, text: str, role: Role) -> str:
        prefix = self.config.query_prefix if role == "query" else self.config.document_prefix
        full = prefix + text
        if self.max_input_chars is not None and len(full) > self.max_input_chars:
            full = full[: self.max_input_chars]
        return full

    def embed(self, texts: Sequence[str], role: Role) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {
            "model": self.config.model,
            "input": [self.prepare(t, role) for t in texts],
            "encoding_format": "float",
        }
        if self.config.dimensions is not None:
            payload["dimensions"] = self.config.dimensions
        body = self.json("POST", "/embeddings", json=payload)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise invalid_response(self.name, "missing 'data' list")
        if len(data) != len(texts):
            raise invalid_response(self.name, f"sent {len(texts)} inputs, received {len(data)} embeddings")
        ordered: list[list[float] | None] = [None] * len(texts)
        for position, item in enumerate(data):
            if not isinstance(item, dict):
                raise invalid_response(self.name, "each data item must be an object")
            index = item.get("index", position)
            vector = item.get("embedding")
            if not isinstance(index, int) or not 0 <= index < len(texts) or ordered[index] is not None:
                raise invalid_response(self.name, f"bad or duplicate index {index!r}")
            if not isinstance(vector, list) or not vector:
                raise invalid_response(self.name, "each embedding must be a non-empty list of numbers")
            try:
                floats = [float(x) for x in vector]
            except (TypeError, ValueError):
                raise invalid_response(self.name, "embeddings must contain numbers only") from None
            if not all(math.isfinite(x) for x in floats):
                raise invalid_response(self.name, "embeddings must be finite")
            ordered[index] = floats
        vectors = [v for v in ordered if v is not None]
        dims = {len(v) for v in vectors}
        if len(dims) != 1:
            raise invalid_response(self.name, f"embeddings have differing lengths {sorted(dims)}")
        if self.config.dimensions is not None and dims != {self.config.dimensions}:
            raise invalid_response(
                self.name, f"requested {self.config.dimensions} dimensions, received {next(iter(dims))}"
            )
        if self.config.normalize:
            vectors = [l2_normalise(v) for v in vectors]
        return vectors


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> OpenAICompatibleEmbedder:
    assert isinstance(config, OpenAICompatibleConfig)
    return OpenAICompatibleEmbedder(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="embedder",
    type="openai_compatible",
    label="OpenAI-compatible embeddings",
    description="Any server exposing POST /embeddings.",
    config_model=OpenAICompatibleConfig,
    fingerprint_fields=("model", "dimensions", "normalize", "query_prefix", "document_prefix", "revision"),
    secret_keys=("api_key",),
    factory=_factory,
)
