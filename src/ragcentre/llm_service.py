"""Cache-through LLM calls for enrich nodes (section 19).

Every request is looked up in ``cache.db`` by ``(llm provider_fp, request key)``. Misses are sent
in parallel, up to the provider's ``max_concurrency``, and each answer is written to the cache as
soon as it returns, so a retry after a failure (or a second corpus with the same node) does not
pay for the same call twice.

Spending limits come from the provider's operational settings and are checked before any call:

- ``max_tokens_per_run``: the estimated tokens of one document's uncached requests.
- ``daily_token_budget``: tokens recorded in ``llm_usage`` for the provider on the current UTC day,
  plus this run's estimate. Concurrent runs check the budget independently, so it can be exceeded
  by at most the runs in flight at once.

Both failures are permanent; the run can be retried once the limit is raised or the day changes.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ragcentre.enrichment import CHARS_PER_TOKEN, Request
from ragcentre.errors import ExecutionError
from ragcentre.providers.base import Completion, invalid_response

if TYPE_CHECKING:
    from ragcentre.context import AppContext

LOOKUP_CHUNK = 500
BUDGET_EXCEEDED = "llm_budget_exceeded"


@dataclass
class LlmResult:
    texts: list[str]
    n_requests: int = 0
    n_cache_hits: int = 0
    n_calls: int = 0
    tokens: int = 0
    stats: dict[str, Any] = field(default_factory=dict)


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _tokens_used(request: Request, completion: Completion) -> int:
    used_in = completion.input_tokens
    if used_in is None:
        used_in = (len(request.system or "") + len(request.prompt)) // CHARS_PER_TOKEN + 1
    used_out = completion.output_tokens
    if used_out is None:
        used_out = len(completion.text) // CHARS_PER_TOKEN + 1
    return used_in + used_out


class LlmService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def _lookup(self, llm_fp: str, keys: Sequence[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        unique = list(dict.fromkeys(keys))
        for start in range(0, len(unique), LOOKUP_CHUNK):
            part = unique[start : start + LOOKUP_CHUNK]
            marks = ",".join("?" for _ in part)
            rows = self.ctx.cache.all(
                f"SELECT request_sha256, text FROM llm_responses WHERE llm_fp = ? AND request_sha256 IN ({marks})",
                [llm_fp, *part],
            )
            for row in rows:
                found[row["request_sha256"]] = str(row["text"])
        return found

    def _store(self, llm_fp: str, key: str, completion: Completion, provider_id: str, tokens: int) -> None:
        with self.ctx.cache.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_responses (llm_fp, request_sha256, text, input_tokens, output_tokens) "
                "VALUES (?, ?, ?, ?, ?)",
                (llm_fp, key, completion.text, completion.input_tokens, completion.output_tokens),
            )
            conn.execute(
                "INSERT INTO llm_usage (provider_id, day, tokens) VALUES (?, ?, ?) "
                "ON CONFLICT (provider_id, day) DO UPDATE SET tokens = tokens + excluded.tokens",
                (provider_id, _today(), tokens),
            )

    def used_today(self, provider_id: str) -> int:
        value = self.ctx.cache.scalar(
            "SELECT tokens FROM llm_usage WHERE provider_id = ? AND day = ?", (provider_id, _today())
        )
        return int(value or 0)

    def _check_budget(self, provider: Any, misses: Sequence[Request]) -> int:
        estimate = sum(r.estimated_tokens() for r in misses)
        per_run = provider.config.get("max_tokens_per_run")
        if per_run is not None and estimate > int(per_run):
            raise ExecutionError(
                BUDGET_EXCEEDED,
                f"Enriching this document needs about {estimate} tokens; {provider.name} allows "
                f"{per_run} per document (max_tokens_per_run). Raise the limit or lower max_doc_chars.",
                provider=provider.name,
            )
        daily = provider.config.get("daily_token_budget")
        if daily is not None:
            used = self.used_today(provider.id)
            if used + estimate > int(daily):
                raise ExecutionError(
                    BUDGET_EXCEEDED,
                    f"{provider.name} has used {used} of its {daily} tokens today (UTC) and this document "
                    f"needs about {estimate} more. Retry tomorrow or raise daily_token_budget.",
                    provider=provider.name,
                )
        return estimate

    def complete_all(self, provider_id: str, requests: Sequence[Request]) -> LlmResult:
        provider = self.ctx.providers.get(provider_id)
        if provider.kind != "llm":
            raise ExecutionError("provider_error", f"{provider.name} is not an LLM provider", provider=provider.name)
        keys = [r.key() for r in requests]
        cached = self._lookup(provider.fingerprint, keys)
        result = LlmResult(texts=[], n_requests=len(requests))
        misses: dict[str, Request] = {}
        for key, request in zip(keys, requests, strict=True):
            if key not in cached and key not in misses:
                misses[key] = request
        result.n_cache_hits = sum(1 for key in keys if key in cached)

        if misses:
            result.stats["estimated_tokens"] = self._check_budget(provider, list(misses.values()))
            started = time.perf_counter()
            with self.ctx.providers.lease(provider_id) as llm:

                def call(item: tuple[str, Request]) -> tuple[str, str, int]:
                    key, request = item
                    completion = llm.complete(request.system, request.prompt, request.max_tokens, request.temperature)
                    if not isinstance(completion, Completion):
                        raise invalid_response(provider.name, "the LLM client returned no completion")
                    tokens = _tokens_used(request, completion)
                    self._store(provider.fingerprint, key, completion, provider.id, tokens)
                    return key, completion.text, tokens

                workers = max(1, min(len(misses), int(getattr(llm, "max_concurrency", 4) or 4)))
                if workers == 1:
                    answers = [call(item) for item in misses.items()]
                else:
                    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ragcentre-llm") as pool:
                        answers = list(pool.map(call, misses.items()))
            for key, text, tokens in answers:
                cached[key] = text
                result.tokens += tokens
            result.n_calls = len(answers)
            result.stats["llm_ms"] = round((time.perf_counter() - started) * 1000, 2)

        result.texts = [cached[key] for key in keys]
        return result
