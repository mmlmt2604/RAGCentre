from __future__ import annotations

import copy
from typing import Any

from ragcentre.context import AppContext
from ragcentre.hashing import pipeline_fp
from ragcentre.pipeline.graph import Resolution
from ragcentre.pipeline.planner import resolve_for_corpus
from ragcentre.retrieval import prompts
from ragcentre.testing.fakes import LLM_URL
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
