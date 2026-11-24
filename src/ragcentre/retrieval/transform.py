"""Query transforms: turn the user's query into the probes the engine searches.

``plan`` is the only entry point. Without a transform (or when a request opts out) it returns the
original query as a single probe, so the engine has one code path. With one, it asks the pipeline's
``llm`` provider for rewrites, paraphrases, hypothetical passages or sub-questions, validates the
answer strictly, and caches the validated list in ``cache.db``. There is no fallback to the raw
query and no retry: a failure is a 502 naming the provider.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ragcentre.hashing import sha256_text
from ragcentre.providers.base import LLM, Message, Role, invalid_response
from ragcentre.retrieval.calls import provider_call
from ragcentre.retrieval.prompts import max_tokens, system_prompt
from ragcentre.timeutil import now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext

ProbeKind = Literal["original", "rewrite", "paraphrase", "hypothetical", "sub_question"]

PROBE_KIND: dict[str, ProbeKind] = {
    "rewrite": "rewrite",
    "multi_query": "paraphrase",
    "hyde": "hypothetical",
    "decompose": "sub_question",
}
MAX_PROBE_CHARS = 1000


@dataclass(frozen=True)
class Probe:
    index: int  # 0 is the original query, even when it is not searched; generated probes start at 1
    text: str
    kind: ProbeKind
    role: Role  # "query", or "document" for hypothetical passages
    vector: bool  # whether vector sources search it
    keyword: bool  # whether keyword sources search it
    group: int  # the decomposition group (its own index); 0 for every other mode


@dataclass(frozen=True)
class QueryPlan:
    mode: str | None
    probes: list[Probe]
    cached: bool
    llm_ms: float | None


def _original(text: str, *, vector: bool = True, keyword: bool = True) -> Probe:
    return Probe(0, text, "original", "query", vector, keyword, 0)


def parse_queries(provider: str, content: str, original: str, limit: int, finish_reason: str) -> list[str]:
    """Validate the model's answer and return up to ``limit`` queries that differ from the original.

    An answer that only repeats the original (a model told to return a simple question as it is)
    gives an empty list: nothing new to search, and nothing wrong. An answer with no text at all is
    an error.
    """
    try:
        data = json.loads(content)
    except ValueError:
        detail = "the answer is not valid JSON"
        if finish_reason == "length":
            detail += " because it hit the token limit; raise max_tokens or use a model that does not reason first"
        raise invalid_response(provider, detail) from None
    queries = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
        raise invalid_response(provider, 'expected a JSON object {"queries": [strings]}')
    seen = {original.strip().casefold()}
    out: list[str] = []
    usable = 0
    for query in queries:
        cleaned = query.strip()[:MAX_PROBE_CHARS].strip()
        if not cleaned:
            continue
        usable += 1
        key = cleaned.casefold()
        if key in seen or len(out) == limit:
            continue
        seen.add(key)
        out.append(cleaned)
    if not usable:
        raise invalid_response(provider, "the answer contained no usable queries")
    return out


def _cache_get(ctx: AppContext, transform_fp: str, query_sha: str) -> list[str] | None:
    row = ctx.cache.one(
        "SELECT output FROM query_transforms WHERE transform_fp = ? AND query_sha256 = ?", (transform_fp, query_sha)
    )
    if row is None:
        return None
    try:
        value = json.loads(row["output"])
    except ValueError:
        return None
    return value if isinstance(value, list) and all(isinstance(v, str) for v in value) else None


def _cache_put(ctx: AppContext, transform_fp: str, query_sha: str, queries: list[str]) -> None:
    # Its own short transaction, after the LLM call. Concurrent identical queries both succeed.
    with ctx.cache.write() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO query_transforms (transform_fp, query_sha256, output, created_at) "
            "VALUES (?, ?, ?, ?)",
            (transform_fp, query_sha, json.dumps(queries, ensure_ascii=False), now_iso()),
        )


def _generate(ctx: AppContext, t: dict[str, Any], text: str) -> list[str]:
    provider_id = t["provider_id"]
    name = ctx.providers.get(provider_id).name
    messages = [Message("system", system_prompt(t["mode"], int(t["n"]), t["instructions"])), Message("user", text)]

    def call() -> list[str]:
        with ctx.providers.lease(provider_id) as llm:
            assert isinstance(llm, LLM)
            completion = llm.complete(messages, max_tokens=max_tokens(t["mode"], int(t["n"])), json_output=True)
        return parse_queries(name, completion.text, text, int(t["n"]), completion.finish_reason)

    return provider_call(ctx, provider_id, call)


def plan(
    ctx: AppContext,
    transform: dict[str, Any] | None,
    text: str,
    *,
    enabled: bool = True,
    has_keyword_source: bool = True,
) -> QueryPlan:
    """The probes to search for ``text``. ``transform`` is the runtime form from the pipeline."""
    if transform is None or not enabled:
        return QueryPlan(None, [_original(text)], False, None)
    mode = transform["mode"]
    query_sha = sha256_text(text)
    generated = _cache_get(ctx, transform["fp"], query_sha)
    cached = generated is not None
    llm_ms: float | None = None
    if generated is None:
        started = time.perf_counter()
        generated = _generate(ctx, transform, text)
        llm_ms = round((time.perf_counter() - started) * 1000, 2)
        _cache_put(ctx, transform["fp"], query_sha, generated)

    if not generated:
        # The model had nothing new to add, so search what was asked, on every source.
        return QueryPlan(mode, [_original(text)], cached, llm_ms)
    hyde = mode == "hyde"
    probes: list[Probe] = []
    if transform["include_original"]:
        probes.append(_original(text))
    elif hyde and has_keyword_source:
        # Keyword sources never see passages, so they keep the original even when it is excluded.
        probes.append(_original(text, vector=False))
    kind = PROBE_KIND[mode]
    for i, item in enumerate(generated, start=1):
        probes.append(
            Probe(
                i,
                item,
                kind,
                "document" if hyde else "query",
                vector=True,
                keyword=not hyde,
                group=i if mode == "decompose" else 0,
            )
        )
    return QueryPlan(mode, probes, cached, llm_ms)
