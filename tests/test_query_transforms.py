from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.errors import ProviderFailed
from ragcentre.eval.runner import run_eval
from ragcentre.hashing import pipeline_fp, sha256_text
from ragcentre.pipeline.graph import Resolution
from ragcentre.pipeline.planner import create_pipeline, resolve_for_corpus
from ragcentre.retrieval import engine, prompts
from ragcentre.retrieval.engine import query
from ragcentre.retrieval.fusion import interleave
from ragcentre.retrieval.transform import Probe, QueryPlan, plan
from ragcentre.testing.fakes import LLM_URL, RERANKER_URL, FakeServices
from test_m5_pipelines import _add_providers, _base_pipeline
from test_m5_pipelines import _corpus as _m5_corpus
from test_m6_ingestion import _corpus, _pipeline, _upload
from test_m7_retrieval import drain
from test_m10_cli import run, server  # noqa: F401 (server is a fixture)


def _setup(ctx: AppContext) -> str:
    _add_providers(ctx)
    ctx.providers.create("chat", "llm", "openai_compatible", {"base_url": LLM_URL, "model": "local-chat"})
    return _m5_corpus(ctx)


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
    assert set(res.retrieval_runtime) == {"sources", "enrich", "fusion", "rerank", "expand", "top_k"}
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


@pytest.mark.parametrize("mode", ["rewrite", "multi_query", "decompose", "hyde"])
@pytest.mark.parametrize("include_original", [True, False])
def test_an_answer_that_only_repeats_the_query_searches_the_original(
    ctx: AppContext, fakes: FakeServices, mode: str, include_original: bool
) -> None:
    runtime = _runtime(ctx, mode=mode, include_original=include_original)
    fakes.script("llm", CHAT, [fakes.llm_reply('{"queries": ["Fast Car", "fast car"]}')])
    result = plan(ctx, runtime, "fast car")
    assert [(p.index, p.kind) for p in result.probes] == [(0, "original")]
    assert result.probes[0].vector and result.probes[0].keyword
    again = plan(ctx, runtime, "fast car")  # an empty answer is cached too
    assert again.cached is True and fakes.count("llm", CHAT) == 1


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


# ---- engine: every mode end to end -------------------------------------------------------------

DOCS = {
    "engine": b"# Engine\n\nA quick automobile accelerates strongly on the highway.",
    "pump": b"# Pump\n\nThe pump seal cracked under thermal stress during the second shift.",
    "garden": b"# Garden\n\nTomatoes grow best with regular watering and plenty of sunlight.",
    "tax": b"# Tax\n\nTax rules change every year for small businesses.",
    "filing": b"# Filing\n\nFiling dates fall at the end of the quarter.",
    "penalty": b"# Penalties\n\nPenalties apply when a report arrives late.",
    "tax2": b"# Tax two\n\nTax rules also differ between regions and sectors.",
    "tax3": b"# Tax three\n\nTax rules for foreign income are covered separately.",
}
PASSAGE = "The pump seal cracked under thermal stress during the second shift."


class Env:
    def __init__(self, ctx: AppContext, client: TestClient, fakes: FakeServices) -> None:
        self.ctx, self.client, self.fakes = ctx, client, fakes
        ctx.providers.create("hash", "embedder", "builtin_hash", {})
        ctx.providers.create("store", "vector_store", "sqlite_vec", {})
        ctx.providers.create("chat", "llm", "openai_compatible", {"base_url": LLM_URL, "model": "local-chat"})
        ctx.providers.create("ranker", "reranker", "http", {"base_url": RERANKER_URL, "model": "test"})
        self.corpus = _corpus(ctx)
        for name, content in DOCS.items():
            _upload(client, self.corpus, content, external_id=name)
        self.count = 0
        self.base = self.make(None)
        drain(ctx)

    def make(self, transform: dict[str, Any] | None, **retrieval: Any) -> str:
        cfg = _pipeline()
        self.count += 1
        cfg["name"] = f"p{self.count}"
        cfg["retrieval"] = {**cfg["retrieval"], **retrieval}
        if transform is not None:
            cfg["retrieval"]["transform"] = {"provider": "chat", **transform}
        pid = create_pipeline(self.ctx, self.corpus, cfg)
        drain(self.ctx)
        return pid

    def ask(self, pid: str, text: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("top_k", 8)
        return query(self.ctx, self.corpus, pid, text, **kwargs)

    def names(self, response: dict[str, Any]) -> list[str]:
        return [r["document"]["external_id"] for r in response["results"]]


@pytest.fixture
def env(ctx: AppContext, client: TestClient, fakes: FakeServices) -> Env:
    return Env(ctx, client, fakes)


DENSE = {"sources": [{"id": "dense", "index": "vec", "top_k": 20}], "fusion": None}
SPARSE = {"sources": [{"id": "sparse", "index": "fts", "top_k": 20}], "fusion": None}


def test_multi_query_finds_what_the_original_words_miss(env: Env) -> None:
    plain = env.make(None, **SPARSE)
    transformed = env.make({"mode": "multi_query", "n": 2}, **SPARSE)
    assert env.ask(plain, "fast car")["results"] == []
    response = env.ask(transformed, "fast car")
    assert env.names(response) == ["engine"]
    info = response["transform"]
    assert info["mode"] == "multi_query" and info["cached"] is False
    assert [(p["index"], p["kind"]) for p in info["probes"]] == [(0, "original"), (1, "paraphrase"), (2, "paraphrase")]
    assert "transform" in response["timings_ms"]
    # One rank per source, as before: the probe lists were merged inside the source.
    assert response["results"][0]["sources"]["sparse"]["rank"] == 1


def test_rewrite_searches_the_rewrite(env: Env) -> None:
    pid = env.make({"mode": "rewrite"}, **SPARSE)
    response = env.ask(pid, "fast car")
    assert env.names(response) == ["engine"]
    assert [p["kind"] for p in response["transform"]["probes"]] == ["original", "rewrite"]


def test_hyde_finds_the_document_a_passage_resembles(env: Env, fakes: FakeServices) -> None:
    fakes.hyde_passage = PASSAGE
    pid = env.make({"mode": "hyde", "n": 1, "include_original": False}, **DENSE)
    response = env.ask(pid, "why did it fail")
    assert env.names(response)[0] == "pump"
    assert response["results"][0]["sources"]["dense"]["rank"] == 1
    assert [p["kind"] for p in response["transform"]["probes"]] == ["hypothetical"]


def test_hyde_passages_are_documents_and_never_reach_keyword_search(
    env: Env, fakes: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes.hyde_passage = PASSAGE
    pid = env.make({"mode": "hyde", "n": 1, "include_original": False})
    seen: list[str] = []
    real = engine._keyword_source

    def spy(ctx: AppContext, corpus_id: str, source: dict[str, Any], text: str, *rest: Any) -> Any:
        seen.append(text)
        return real(ctx, corpus_id, source, text, *rest)

    monkeypatch.setattr(engine, "_keyword_source", spy)
    response = env.ask(pid, "why did it fail")
    assert seen == ["why did it fail"]  # the original, even though include_original is false
    roles = {
        r["role"]
        for r in env.ctx.cache.all("SELECT role FROM embeddings WHERE text_sha256 = ?", (sha256_text(PASSAGE),))
    }
    assert roles == {"document"}
    assert env.names(response)[0] == "pump"


def test_decompose_gives_every_sub_question_a_share(env: Env, fakes: FakeServices) -> None:
    pid = env.make(
        {"mode": "decompose", "n": 3, "include_original": False},
        rerank={"provider": "ranker", "top_n": 20},
        top_k=4,
    )
    text = "tax rules and filing dates and penalties late"
    response = env.ask(pid, text, top_k=4)
    names = env.names(response)
    # Three tax documents match the first sub-question best; the other two still get their turn.
    assert names[:3] == ["tax", "filing", "penalty"]
    assert sum(n.startswith("tax") for n in names) <= 2
    assert [p["text"] for p in response["transform"]["probes"]] == ["tax rules", "filing dates", "penalties late"]
    # Each group is reranked against its own sub-question.
    queries = [json.loads(r.content)["query"] for r in fakes.requests if r.url.path == "/rerank"]
    assert sorted(queries) == ["filing dates", "penalties late", "tax rules"]


def test_interleave_is_fair_and_skips_duplicates() -> None:
    groups = [[("a", 3.0), ("b", 2.0), ("c", 1.0), ("d", 0.5)], [("x", 9.0), ("a", 8.0)], [("y", 1.0)]]
    picked = interleave(groups, 6)
    ids = [c for _, c, _ in picked]
    assert ids == ["a", "x", "y", "b", "c", "d"]
    assert len(set(ids)) == len(ids)
    assert [g for g, _, _ in picked[:3]] == [0, 1, 2]
    assert interleave(groups, 3) == [(0, "a", 3.0), (1, "x", 9.0), (2, "y", 1.0)]
    assert interleave([[], []], 5) == []


def test_hybrid_pipeline_keeps_source_weights_and_ranks(env: Env) -> None:
    pid = env.make({"mode": "multi_query", "n": 2}, fusion={"method": "rrf", "k": 60, "weights": {"dense": 2.0}})
    response = env.ask(pid, "fast car")
    assert env.names(response)[0] == "engine"
    for result in response["results"]:
        for source in result["sources"].values():
            assert 1 <= source["rank"] <= 30


def test_opting_out_skips_the_llm_and_matches_the_plain_pipeline(env: Env, fakes: FakeServices) -> None:
    pid = env.make({"mode": "multi_query", "n": 3})
    before = fakes.count("llm")
    skipped = env.ask(pid, "tax rules", transform=False)
    assert fakes.count("llm") == before
    assert skipped["transform"] is None and "transform" not in skipped["timings_ms"]
    plain = env.ask(env.base, "tax rules")
    key = lambda r: [(x["chunk_id"], x["score"], x["rank"], x["sources"]) for x in r["results"]]  # noqa: E731
    assert key(skipped) == key(plain)
    assert env.ask(pid, "tax rules", transform=True)["transform"] is not None


def test_debug_marks_which_probes_found_each_result(env: Env) -> None:
    pid = env.make({"mode": "multi_query", "n": 2}, **SPARSE)
    response = env.ask(pid, "fast car", debug=True)
    assert response["results"][0]["probes"] == [1]  # only the paraphrase "quick automobile" matches
    assert "probes" not in env.ask(pid, "fast car")["results"][0]


def test_many_sources_and_probes_do_not_deadlock_the_pool(env: Env) -> None:
    sources = [
        {"id": "a", "index": "vec", "top_k": 20},
        {"id": "b", "index": "vec", "top_k": 20},
        {"id": "c", "index": "fts", "top_k": 20},
        {"id": "d", "index": "fts", "top_k": 20},
    ]
    pid = env.make({"mode": "multi_query", "n": 8}, sources=sources, fusion={"method": "rrf", "k": 60})
    with ThreadPoolExecutor(1) as pool:
        response = pool.submit(env.ask, pid, "fast car").result(timeout=30)
    assert len(response["transform"]["probes"]) == 9
    assert env.names(response)[0] == "engine"


def test_failures_return_no_partial_results(env: Env, fakes: FakeServices) -> None:
    pid = env.make({"mode": "multi_query", "n": 2})
    fakes.script("llm", CHAT, [500])
    with pytest.raises(ProviderFailed) as info:
        env.ask(pid, "fast car")
    assert info.value.details["provider"] == "chat"
    assert fakes.count("llm", CHAT) == 1
    assert env.ask(pid, "fast car")["results"]  # the next query works; the failure was not cached


def test_clearing_the_transform_cache(env: Env, fakes: FakeServices, client: TestClient) -> None:
    pid = env.make({"mode": "multi_query", "n": 2})
    env.ask(pid, "fast car")
    env.ask(pid, "big error")
    calls = fakes.count("llm", CHAT)
    assert env.ask(pid, "fast car")["transform"]["cached"] is True
    assert fakes.count("llm", CHAT) == calls
    assert client.post("/v1/admin/cache/clear", json={"transforms": False}).status_code == 422
    cleared = client.post("/v1/admin/cache/clear", json={"transforms": True})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"transforms_deleted": 2}
    assert env.ask(pid, "fast car")["transform"]["cached"] is False
    assert fakes.count("llm", CHAT) == calls + 1


def test_cli_cache_clear(server: str) -> None:  # noqa: F811
    code, out = run(server, "cache", "clear", "--transforms")
    assert code == 0 and "Cleared 0 cached query transforms." in out
    code, out = run(server, "--json", "cache", "clear", "--transforms")
    assert code == 0 and json.loads(out) == {"transforms_deleted": 0}
    code, out = run(server, "cache", "clear")
    assert code == 2 and "Choose what to clear" in out


# ---- API and CLI -------------------------------------------------------------------------------


def _post(env: Env, pid: str, **body: Any) -> dict[str, Any]:
    response = env.client.post(
        f"/v1/corpora/{env.corpus}/query", json={"pipeline_id": pid, "query": "fast car", "top_k": 5, **body}
    )
    assert response.status_code == 200, response.text
    out: dict[str, Any] = response.json()
    return out


def test_api_returns_the_probes_and_honours_the_opt_out(env: Env, fakes: FakeServices) -> None:
    pid = env.make({"mode": "multi_query", "n": 2}, **SPARSE)
    body = _post(env, pid)
    assert body["transform"] == {
        "mode": "multi_query",
        "probes": [
            {"index": 0, "text": "fast car", "kind": "original"},
            {"index": 1, "text": "quick automobile", "kind": "paraphrase"},
            {"index": 2, "text": "car fast", "kind": "paraphrase"},
        ],
        "cached": False,
    }
    assert body["results"][0]["probes"] is None  # only with debug
    assert _post(env, pid)["transform"]["cached"] is True
    calls = fakes.count("llm", CHAT)
    skipped = _post(env, pid, transform=False)
    assert skipped["transform"] is None and skipped["results"] == []
    assert fakes.count("llm", CHAT) == calls
    assert _post(env, pid, debug=True)["results"][0]["probes"] == [1]
    assert _post(env, env.base)["transform"] is None


def test_api_reports_a_failing_model_as_502(env: Env, fakes: FakeServices) -> None:
    pid = env.make({"mode": "rewrite"}, **SPARSE)
    fakes.script("llm", CHAT, [fakes.llm_reply("nope")])
    response = env.client.post(f"/v1/corpora/{env.corpus}/query", json={"pipeline_id": pid, "query": "fast car"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert "chat" in response.json()["error"]["message"]


def test_cli_prints_the_probes_and_can_skip_them(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    from typer.testing import CliRunner

    from ragcentre import cli

    sent: list[dict[str, Any]] = []
    transform = {
        "mode": "multi_query",
        "cached": True,
        "probes": [
            {"index": 0, "text": "fast car", "kind": "original"},
            {"index": 1, "text": "quick\nautomobile", "kind": "paraphrase"},
        ],
    }

    class FakeClient:
        def corpus(self, ref: str) -> dict[str, Any]:
            return {"id": "c1"}

        def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
            sent.append(json)
            return {
                "partial": False,
                "results": [],
                "timings_ms": {"total": 3.0},
                "transform": None if json.get("transform") is False else transform,
            }

    @contextmanager
    def fake_api() -> Any:
        yield FakeClient()

    monkeypatch.setattr(cli, "_api", fake_api)
    shown = CliRunner().invoke(cli.app, ["query", "docs", "fast car"])
    assert shown.exit_code == 0, shown.output
    assert "Query transform: multi_query (cached)" in shown.output
    assert "[0] original: fast car" in shown.output and "[1] paraphrase: quick automobile" in shown.output
    assert "transform" not in sent[0]
    skipped = CliRunner().invoke(cli.app, ["query", "docs", "fast car", "--no-transform"])
    assert "Query transform" not in skipped.output and sent[1]["transform"] is False


# ---- evaluation --------------------------------------------------------------------------------


def test_an_evaluation_compares_a_transformed_pipeline_with_the_same_one_without(env: Env, fakes: FakeServices) -> None:
    cid, client = env.corpus, env.client
    plain = env.make(None, **SPARSE)
    transformed = env.make({"mode": "multi_query", "n": 2}, **SPARSE)
    items = [{"question": "fast car", "targets": [{"document": "engine"}]}]
    body = "\n".join(json.dumps(item) for item in items).encode()
    dataset = client.post(
        f"/v1/corpora/{cid}/eval/datasets", data={"name": "qa"}, files={"file": ("qa.jsonl", body, "application/jsonl")}
    ).json()

    def run() -> dict[str, Any]:
        started = client.post(
            "/v1/eval/runs", json={"dataset_id": dataset["id"], "pipeline_ids": [plain, transformed], "k_values": [3]}
        )
        assert started.status_code == 201, started.text
        run_eval(env.ctx, started.json()["id"])
        detail: dict[str, Any] = client.get(f"/v1/eval/runs/{started.json()['id']}").json()
        assert detail["status"] == "finished"
        return detail

    first = run()
    metrics = {s["pipeline_id"]: s["metrics"] for s in first["summaries"]}
    assert metrics[plain]["hit_rate@3"] == 0.0
    assert metrics[transformed]["hit_rate@3"] == 1.0
    assert all(s["error_count"] == 0 for s in first["summaries"])
    assert fakes.count("llm", CHAT) == 1
    # The same questions again: the cached rewrites make the run repeatable and free.
    second = run()
    assert fakes.count("llm", CHAT) == 1
    assert {s["pipeline_id"]: s["metrics"] for s in second["summaries"]} == metrics


# ---- examples ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["multi-query", "hyde"])
def test_example_pipelines_are_valid_and_share_production_indexes(ctx: AppContext, name: str) -> None:
    import yaml  # the CLI reads these files with PyYAML

    root = Path(__file__).resolve().parent.parent / "examples" / "pipelines"
    ctx.providers.create("docling", "parser", "http", {"base_url": "http://parser.test"})
    ctx.providers.create("openai-small", "embedder", "builtin_hash", {})
    ctx.providers.create("local-sqlite", "vector_store", "sqlite_vec", {})
    ctx.providers.create("reranker", "reranker", "http", {"base_url": RERANKER_URL, "model": "test"})
    ctx.providers.create("ollama-chat", "llm", "openai_compatible", {"base_url": LLM_URL, "model": "llama3.2"})
    corpus = _m5_corpus(ctx)
    production = _resolve(ctx, corpus, yaml.safe_load((root / "production.yaml").read_text()))
    example = _resolve(ctx, corpus, yaml.safe_load((root / f"{name}.yaml").read_text()))
    assert production.valid, production.problems
    assert example.valid, example.problems
    assert [n.node_fp for n in example.nodes] == [n.node_fp for n in production.nodes]
    assert example.retrieval_runtime["transform"]["mode"] == name.replace("-", "_")
