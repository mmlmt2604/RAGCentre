"""Cache-through embedding (section 12.4).

Every text is looked up in ``cache.db`` by ``(embedder provider_fp, role, sha256(text))``. Misses
are embedded in batches of the provider's ``batch_size``, and each batch is written to the cache
as soon as it returns, so a retry after a failure resumes where it stopped. Vectors come back
in input order.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ragcentre.errors import ExecutionError
from ragcentre.hashing import sha256_text
from ragcentre.providers.base import Role, invalid_response
from ragcentre.vectors import float32, pack, unpack

if TYPE_CHECKING:
    from ragcentre.context import AppContext

LOOKUP_CHUNK = 500


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    n_texts: int = 0
    n_cache_hits: int = 0
    n_truncated: int = 0
    n_requests: int = 0
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def dim(self) -> int | None:
        return len(self.vectors[0]) if self.vectors else None


class EmbeddingService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def _lookup(self, embedder_fp: str, role: str, shas: Sequence[str]) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        unique = list(dict.fromkeys(shas))
        for start in range(0, len(unique), LOOKUP_CHUNK):
            part = unique[start : start + LOOKUP_CHUNK]
            marks = ",".join("?" for _ in part)
            rows = self.ctx.cache.all(
                f"SELECT text_sha256, vector FROM embeddings WHERE embedder_fp = ? AND role = ? "
                f"AND text_sha256 IN ({marks})",
                [embedder_fp, role, *part],
            )
            for row in rows:
                found[row["text_sha256"]] = unpack(bytes(row["vector"]))
        return found

    def _store(self, embedder_fp: str, role: str, items: Sequence[tuple[str, list[float]]]) -> None:
        with self.ctx.cache.write() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings (embedder_fp, role, text_sha256, dim, vector) VALUES (?, ?, ?, ?, ?)",
                [(embedder_fp, role, sha, len(vec), pack(vec)) for sha, vec in items],
            )

    def embed(self, provider_id: str, role: Role, texts: Sequence[str]) -> EmbedResult:
        provider = self.ctx.providers.get(provider_id)
        if provider.kind != "embedder":
            raise ExecutionError("provider_error", f"{provider.name} is not an embedder", provider=provider.name)
        shas = [sha256_text(t) for t in texts]
        cached = self._lookup(provider.fingerprint, role, shas)
        result = EmbedResult(vectors=[], n_texts=len(texts))

        misses: dict[str, str] = {}
        for sha, text in zip(shas, texts, strict=True):
            if sha not in cached and sha not in misses:
                misses[sha] = text
        result.n_cache_hits = sum(1 for sha in shas if sha in cached)

        if misses:
            embedder = self.ctx.providers.instance(provider_id)
            batch_size = max(1, int(getattr(embedder, "batch_size", 64)))
            limit = getattr(embedder, "max_input_chars", None)
            items = list(misses.items())
            started = time.perf_counter()
            for start in range(0, len(items), batch_size):
                batch = items[start : start + batch_size]
                vectors = embedder.embed([text for _, text in batch], role)
                result.n_requests += 1
                if len(vectors) != len(batch):
                    raise invalid_response(provider.name, f"sent {len(batch)} texts, got {len(vectors)} vectors")
                rounded = [float32(v) for v in vectors]
                self._store(
                    provider.fingerprint, role, [(sha, vec) for (sha, _), vec in zip(batch, rounded, strict=True)]
                )
                for (sha, _), vec in zip(batch, rounded, strict=True):
                    cached[sha] = vec
            result.stats["embed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            if limit is not None:
                result.n_truncated = sum(1 for t in misses.values() if len(t) > int(limit))

        result.vectors = [cached[sha] for sha in shas]
        dims = {len(v) for v in result.vectors}
        if len(dims) > 1:
            raise ExecutionError(
                "embedding_dimension_mismatch",
                f"{provider.name} returned vectors of differing lengths {sorted(dims)} for one request; "
                "clear the embedding cache if the model changed under the same provider.",
                provider=provider.name,
            )
        return result
