from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from ragcentre.context import AppContext
from ragcentre.errors import ProviderFailed
from ragcentre.hashing import pipeline_fp
from ragcentre.pipeline.graph import Resolution
from ragcentre.pipeline.planner import resolve_for_corpus
from ragcentre.retrieval import prompts
from ragcentre.retrieval.transform import Probe, QueryPlan, plan
from ragcentre.testing.fakes import LLM_URL, FakeServices
from test_m5_pipelines import _add_providers, _base_pipeline, _corpus


def _setup(ctx: AppContext) -> str:
    _add_providers(ctx)
    ctx.providers.create("chat", "llm", "openai_compatible", {"base_url": LLM_URL, "model": "local-chat"})
    return _corpus(ctx)


def _with_transform(**overrides: Any) -> dict[str, Any]:
    raw = _base_pipeline()
    raw["retrieval"]["transform"] = {"mode": "multi_query", "provider": "chat", **overrides}
    return raw


def _resolve(ctx: AppContext, corpus: str, raw: dict[str, Any]) -> Resolution:
    return resolve_for_corpus(ctx, corpus, raw)


def _problems(res: Resolution) -> dict[str, str]:
    return {p.path: p.message for p in res.problems}


def test_pipeline_without_transform_keeps_its_fingerprint(ctx: AppContext) -> None:
    # Provider fingerprints differ per install, so the absolute value cannot be pinned. What must
    # hold is that the retrieval form is exactly the pre-transform shape and the pipeline
    # fingerprint is derived from it alone.
    corpus = _setup(ctx)
    res = _resolve(ctx, corpus, _base_pipeline())
    assert res.valid
    assert set(res.retrieval) == {"sources", "fusion", "rerank", "expand", "top_k"}
    assert set(res.retrieval_runtime) == {"sources", "fusion", "rerank", "expand", "top_k"}
    assert res.fingerprint == pipeline_fp([n.node_fp for n in res.nodes], res.retrieval)


def test_transform_resolves_and_changes_only_the_retrieval_fingerprint(ctx: AppContext) -> None:
    corpus = _setup(ctx)
    plain = _resolve(ctx, corpus, _base_pipeline())
    res = _resolve(ctx, corpus, _with_transform())
    assert res.valid, res.problems
    assert res.fingerprint != plain.fingerprint
    assert [n.node_fp for n in res.nodes] == [n.node_fp for n in plain.nodes]
    llm = ctx.providers.by_name("chat")
    assert llm is not None
    runtime = res.retrieval_runtime["transform"]
    assert runtime["provider_id"] == llm.id
    assert runtime["mode"] == "multi_query"
    assert runtime["n"] == 3
    assert llm.id in res.provider_ids
    assert "provider_id" not in res.retrieval["transform"]


def test_every_transform_field_enters_the_fingerprint(ctx: AppContext) -> None:
    corpus = _setup(ctx)
    base = _resolve(ctx, corpus, _with_transform()).fingerprint
    variants: list[dict[str, Any]] = [
        {"mode": "rewrite"},
        {"mode": "decompose"},
        {"n": 4},
        {"include_original": False},
        {"original_weight": 2.0},
        {"k": 30},
        {"instructions": "Use legal vocabulary."},
    ]
    seen = {base}
    for change in variants:
        fingerprint = _resolve(ctx, corpus, _with_transform(**change)).fingerprint
        assert fingerprint not in seen, change
        seen.add(fingerprint)


def test_rewrite_ignores_n(ctx: AppContext) -> None:
    corpus = _setup(ctx)
    a = _resolve(ctx, corpus, _with_transform(mode="rewrite", n=1))
    b = _resolve(ctx, corpus, _with_transform(mode="rewrite", n=5))
    assert a.fingerprint == b.fingerprint
    assert a.retrieval["transform"]["n"] == 1


def test_llm_provider_settings_and_impl_version_enter_the_fingerprint(ctx: AppContext, monkeypatch: Any) -> None:
    corpus = _setup(ctx)
    base = _resolve(ctx, corpus, _with_transform()).fingerprint
    monkeypatch.setitem(prompts.TRANSFORM_IMPL, "multi_query", prompts.TRANSFORM_IMPL["multi_query"] + 1)
    assert _resolve(ctx, corpus, _with_transform()).fingerprint != base
    monkeypatch.undo()
    row = ctx.providers.by_name("chat")
    assert row is not None
    ctx.providers.create("chat2", "llm", "openai_compatible", {"base_url": LLM_URL, "model": "other-model"})
    other = _with_transform(provider="chat2")
    assert _resolve(ctx, corpus, other).fingerprint != base
    # Operational settings do not enter the fingerprint.
    ctx.providers.create(
        "chat3", "llm", "openai_compatible", {"base_url": LLM_URL, "model": "local-chat", "timeout_seconds": 5}
    )
    assert _resolve(ctx, corpus, _with_transform(provider="chat3")).fingerprint == base


def test_validation(ctx: AppContext) -> None:
    corpus = _setup(ctx)
    assert _problems(_resolve(ctx, corpus, _with_transform(provider="hash")))["retrieval.transform.provider"]
    assert "retrieval.transform.provider" in _problems(_resolve(ctx, corpus, _with_transform(provider="nope")))
    for field, value in (("n", 0), ("n", 9), ("original_weight", -1), ("original_weight", 11), ("k", 0)):
        assert f"retrieval.transform.{field}" in _problems(_resolve(ctx, corpus, _with_transform(**{field: value})))
    too_long = _problems(_resolve(ctx, corpus, _with_transform(instructions="x" * 2001)))
    assert "retrieval.transform.instructions" in too_long
    assert _resolve(ctx, corpus, _with_transform(instructions="x" * 2000)).valid


def test_hyde_needs_a_vector_source(ctx: AppContext) -> None:
    corpus = _setup(ctx)
    raw = _with_transform(mode="hyde")
    assert _resolve(ctx, corpus, raw).valid
    keyword_only = copy.deepcopy(raw)
    keyword_only["retrieval"]["sources"] = [{"id": "sparse", "index": "fts", "top_k": 30}]
    keyword_only["retrieval"].pop("fusion")
    problems = _problems(_resolve(ctx, corpus, keyword_only))
    assert "vector source" in problems["retrieval.transform.mode"]
    keyword_only["retrieval"]["transform"]["mode"] = "multi_query"
    assert _resolve(ctx, corpus, keyword_only).valid


def test_unknown_mode_and_fields_are_structural_errors(ctx: AppContext) -> None:
    corpus = _setup(ctx)
    assert not _resolve(ctx, corpus, _with_transform(mode="magic")).valid
    assert not _resolve(ctx, corpus, _with_transform(extra=1)).valid


# ---- probes: plan(), validation, cache ---------------------------------------------------------

CHAT = "/chat/completions"


def _runtime(ctx: AppContext, **overrides: Any) -> dict[str, Any]:
    corpus = _setup_once(ctx)
    res = _resolve(ctx, corpus, _with_transform(**overrides))
    assert res.valid, res.problems
    return res.retrieval_runtime["transform"]


def _setup_once(ctx: AppContext) -> str:
    if ctx.providers.by_name("chat") is None:
        return _setup(ctx)
    return str(ctx.db.scalar("SELECT id FROM corpora LIMIT 1"))


def _texts(p: QueryPlan) -> list[str]:
    return [probe.text for probe in p.probes]


def test_plan_without_transform_is_the_original_only(ctx: AppContext, fakes: FakeServices) -> None:
    result = plan(ctx, None, "fast car")
    assert result == QueryPlan(None, [Probe(0, "fast car", "original", "query", True, True, 0)], False, None)
    assert plan(ctx, _runtime(ctx), "fast car", enabled=False).mode is None
    assert fakes.count("llm") == 0


def test_multi_query_probes(ctx: AppContext) -> None:
    result = plan(ctx, _runtime(ctx, n=3), "fast car")
    assert result.mode == "multi_query"
    assert [p.kind for p in result.probes] == ["original", "paraphrase", "paraphrase", "paraphrase"]
    assert _texts(result) == ["fast car", "quick automobile", "car fast", "information about fast car"]
    assert [p.index for p in result.probes] == [0, 1, 2, 3]
    assert all(p.vector and p.keyword and p.role == "query" and p.group == 0 for p in result.probes)
    assert result.cached is False
    assert result.llm_ms is not None


def test_include_original_false_drops_the_original(ctx: AppContext) -> None:
    result = plan(ctx, _runtime(ctx, include_original=False, n=2), "fast car")
    assert _texts(result) == ["quick automobile", "car fast"]
    assert [p.index for p in result.probes] == [1, 2]


def test_rewrite_makes_one_probe(ctx: AppContext) -> None:
    result = plan(ctx, _runtime(ctx, mode="rewrite", n=5), "fast car")
    assert [p.kind for p in result.probes] == ["original", "rewrite"]
    assert result.probes[1].text == "quick automobile"


def test_hyde_probes_are_documents_and_skip_keyword(ctx: AppContext, fakes: FakeServices) -> None:
    fakes.hyde_passage = "The engine failed because of a worn valve."
    result = plan(ctx, _runtime(ctx, mode="hyde", n=1), "why did it fail")
    assert [(p.kind, p.role, p.vector, p.keyword) for p in result.probes] == [
        ("original", "query", True, True),
        ("hypothetical", "document", True, False),
    ]
    assert result.probes[1].text == "The engine failed because of a worn valve."


def test_hyde_without_original_still_feeds_keyword_sources(ctx: AppContext) -> None:
    runtime = _runtime(ctx, mode="hyde", n=1, include_original=False)
    kept = plan(ctx, runtime, "why did it fail", has_keyword_source=True)
    assert [(p.kind, p.vector, p.keyword) for p in kept.probes] == [
        ("original", False, True),
        ("hypothetical", True, False),
    ]
    only_vectors = plan(ctx, runtime, "why did it fail two", has_keyword_source=False)
    assert [p.kind for p in only_vectors.probes] == ["hypothetical"]


def test_decompose_groups(ctx: AppContext) -> None:
    result = plan(ctx, _runtime(ctx, mode="decompose", n=3), "tax rules and filing dates and penalties")
    assert _texts(result) == ["tax rules and filing dates and penalties", "tax rules", "filing dates", "penalties"]
    assert [p.group for p in result.probes] == [0, 1, 2, 3]
    assert all(p.kind == "sub_question" for p in result.probes[1:])


def test_a_second_identical_query_is_served_from_the_cache(ctx: AppContext, fakes: FakeServices) -> None:
    runtime = _runtime(ctx)
    first = plan(ctx, runtime, "fast car")
    assert fakes.count("llm", CHAT) == 1
    second = plan(ctx, runtime, "fast car")
    assert fakes.count("llm", CHAT) == 1
    assert second.cached is True and second.llm_ms is None
    assert _texts(second) == _texts(first)
    plan(ctx, runtime, "big error")
    assert fakes.count("llm", CHAT) == 2
    # Other instructions are another transform, so they miss.
    plan(ctx, _runtime(ctx, instructions="Prefer legal terms."), "fast car")
    assert fakes.count("llm", CHAT) == 3


def test_concurrent_identical_queries_both_succeed(ctx: AppContext, fakes: FakeServices) -> None:
    runtime = _runtime(ctx)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: plan(ctx, runtime, "fast car"), range(4)))
    assert len({tuple(_texts(r)) for r in results}) == 1
    assert ctx.cache.scalar("SELECT COUNT(*) FROM query_transforms") == 1


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"queries": "x"}', '{"queries": [1]}', '{"queries": []}', '{"queries": ["", "  "]}'],
)
def test_unusable_answers_are_a_502_naming_the_provider(ctx: AppContext, fakes: FakeServices, content: str) -> None:
    runtime = _runtime(ctx)
    fakes.script("llm", CHAT, [fakes.llm_reply(content)])
    with pytest.raises(ProviderFailed) as info:
        plan(ctx, runtime, "fast car")
    assert "chat" in info.value.message
    assert fakes.count("llm", CHAT) == 1  # no retry
    assert ctx.cache.scalar("SELECT COUNT(*) FROM query_transforms") == 0  # failures are never cached


def test_truncated_answer_says_why(ctx: AppContext, fakes: FakeServices) -> None:
    runtime = _runtime(ctx)
    fakes.script("llm", CHAT, [fakes.llm_reply('{"queries": ["fast', "length")])
    with pytest.raises(ProviderFailed) as info:
        plan(ctx, runtime, "fast car")
    assert "token limit" in info.value.message
    fakes.script("llm", CHAT, [fakes.llm_reply("", "length")])
    with pytest.raises(ProviderFailed):
        plan(ctx, runtime, "other query")


def test_valid_json_cut_by_the_token_limit_is_accepted(ctx: AppContext, fakes: FakeServices) -> None:
    runtime = _runtime(ctx)
    fakes.script("llm", CHAT, [fakes.llm_reply('{"queries": ["quick car"]}', "length")])
    assert _texts(plan(ctx, runtime, "fast car")) == ["fast car", "quick car"]


@pytest.mark.parametrize("status", [429, 500])
def test_http_failures_are_a_502_without_retry(ctx: AppContext, fakes: FakeServices, status: int) -> None:
    runtime = _runtime(ctx)
    fakes.script("llm", CHAT, [status])
    with pytest.raises(ProviderFailed) as info:
        plan(ctx, runtime, "fast car")
    assert info.value.details["provider"] == "chat"
    assert fakes.count("llm", CHAT) == 1


def test_answers_are_cleaned(ctx: AppContext, fakes: FakeServices) -> None:
    runtime = _runtime(ctx, n=2)
    payload = {"queries": ["  Fast Car ", "FAST CAR", "a" * 1500, "second", "third"]}
    fakes.script("llm", CHAT, [fakes.llm_reply(json.dumps(payload))])
    result = plan(ctx, runtime, "fast car")
    # The original (any case) and duplicates are dropped, long items cut, and only n are kept.
    assert [len(t) for t in _texts(result)] == [len("fast car"), 1000, len("second")]


def test_the_request_carries_the_prompt_and_asks_for_json(ctx: AppContext, fakes: FakeServices) -> None:
    runtime = _runtime(ctx, mode="hyde", n=2, instructions="Corpus: veterinary manuals.")
    plan(ctx, runtime, "sick cat")
    body = json.loads(fakes.requests[-1].content)
    assert body["response_format"] == {"type": "json_object"}
    system, user = body["messages"]
    assert system["content"].startswith("Task: hyde")
    assert "up to 2" in system["content"] and "Corpus: veterinary manuals." in system["content"]
    assert user == {"role": "user", "content": "sick cat"}
    assert body["max_tokens"] == 64 + 2 * 200
