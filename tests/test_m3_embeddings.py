from __future__ import annotations

import threading
import time

import httpx
import pytest

from ragcentre.context import AppContext
from ragcentre.errors import ExecutionError
from ragcentre.providers.base import classify_exception, classify_response, parse_retry_after
from ragcentre.providers.embedders.builtin_hash import hash_embed
from ragcentre.testing.fakes import CHUNKER_URL, EMBEDDER_URL, PARSER_URL, RERANKER_URL, FakeServices
from ragcentre.vectors import cosine_similarity, norm


def embedder(ctx: AppContext, **config: object) -> str:
    row = ctx.providers.create(
        "emb", "embedder", "openai_compatible", {"base_url": EMBEDDER_URL, "model": "m", **config}
    )
    return row.id


def test_second_embedding_makes_no_http_calls(ctx: AppContext, fakes: FakeServices) -> None:
    pid = embedder(ctx, batch_size=3)
    texts = [f"text number {i}" for i in range(10)]
    first = ctx.embeddings.embed(pid, "document", texts)
    assert first.n_cache_hits == 0 and first.n_requests == 4
    assert fakes.count("embedder") == 4
    fakes.reset_counts()
    second = ctx.embeddings.embed(pid, "document", texts)
    assert fakes.count("embedder") == 0
    assert second.n_cache_hits == 10
    assert second.vectors == first.vectors
    # Queries are cached separately from documents.
    ctx.embeddings.embed(pid, "query", texts[:1])
    assert fakes.count("embedder") == 1


def test_vectors_in_input_order_and_duplicates(ctx: AppContext, fakes: FakeServices) -> None:
    pid = embedder(ctx, batch_size=2)
    texts = ["alpha", "beta", "alpha", "gamma", "beta"]
    res = ctx.embeddings.embed(pid, "document", texts)
    assert fakes.embedded_texts == ["alpha", "beta", "gamma"]  # each distinct text once
    for text, vec in zip(texts, res.vectors, strict=True):
        assert cosine_similarity(vec, hash_embed(text, 64)) == pytest.approx(1.0, abs=1e-6)


def test_failure_on_third_batch_keeps_first_two_cached(ctx: AppContext, fakes: FakeServices) -> None:
    pid = embedder(ctx, batch_size=2)
    texts = [f"t{i}" for i in range(7)]
    fakes.fail_on("embedder", "/embeddings", {3})
    with pytest.raises(ExecutionError) as exc:
        ctx.embeddings.embed(pid, "document", texts)
    assert exc.value.retryable
    assert ctx.cache.scalar("SELECT COUNT(*) FROM embeddings") == 4
    fakes.reset_counts()
    fakes.fail_on("embedder", "/embeddings", set())
    res = ctx.embeddings.embed(pid, "document", texts)
    assert res.n_cache_hits == 4
    assert fakes.embedded_texts == ["t4", "t5", "t6"]
    assert fakes.count("embedder") == 2


def test_prefixes_truncation_and_normalise(ctx: AppContext, fakes: FakeServices) -> None:
    pid = embedder(ctx, query_prefix="q: ", document_prefix="d: ", max_input_chars=8, normalize=True, dimensions=16)
    res = ctx.embeddings.embed(pid, "document", ["hello world", "hi"])
    assert fakes.embedded_texts == ["d: hello", "d: hi"]
    assert res.n_truncated == 1
    assert all(norm(v) == pytest.approx(1.0, abs=1e-5) for v in res.vectors)
    assert len(res.vectors[0]) == 16
    ctx.embeddings.embed(pid, "query", ["x"])
    assert fakes.embedded_texts[-1] == "q: x"
    body = fakes.requests[-1].read()
    assert b'"dimensions":16' in body.replace(b" ", b"")
    assert b'"encoding_format":"float"' in body.replace(b" ", b"")


def test_dimensions_not_sent_unless_configured(ctx: AppContext, fakes: FakeServices) -> None:
    pid = embedder(ctx)
    ctx.embeddings.embed(pid, "document", ["x"])
    assert b"dimensions" not in fakes.requests[-1].read()


@pytest.mark.parametrize(
    ("response", "problem"),
    [
        ({"data": []}, "received 0"),
        ({"nodata": 1}, "data"),
        ({"data": [{"index": 0, "embedding": [1, 2]}, {"index": 1, "embedding": [1]}]}, "differing"),
        ({"data": [{"index": 0, "embedding": ["a"]}, {"index": 1, "embedding": [1]}]}, "numbers"),
        ({"data": [{"index": 0, "embedding": [1]}, {"index": 0, "embedding": [1]}]}, "duplicate"),
    ],
)
def test_invalid_embedding_responses_are_permanent(
    ctx: AppContext, fakes: FakeServices, response: dict[str, object], problem: str
) -> None:
    pid = embedder(ctx)
    fakes.script("embedder", "/embeddings", [httpx.Response(200, json=response)])
    with pytest.raises(ExecutionError) as exc:
        ctx.embeddings.embed(pid, "document", ["a", "b"])
    assert not exc.value.retryable
    assert problem in exc.value.message


def test_builtin_hash_embedding(ctx: AppContext) -> None:
    pid = ctx.providers.create("hash", "embedder", "builtin_hash", {"dimensions": 128}).id
    res = ctx.embeddings.embed(pid, "document", ["The warranty period is 24 months", "warranty period", "bananas"])
    a, b, c = res.vectors
    assert len(a) == 128 and norm(a) == pytest.approx(1.0, abs=1e-5)
    assert cosine_similarity(a, b) > cosine_similarity(a, c)
    assert hash_embed("", 8) == [0.0] * 8


# ---- error classification ----------------------------------------------------------------------


def _resp(status: int, headers: dict[str, str] | None = None, body: object = None) -> httpx.Response:
    return httpx.Response(status, headers=headers, json=body or {"error": {"code": "x", "message": "nope"}},
                          request=httpx.Request("POST", "http://svc/x"))  # fmt: skip


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(408, True), (429, True), (500, True), (502, True), (503, True), (504, True),
     (400, False), (401, False), (403, False), (404, False), (413, False), (422, False)],
)  # fmt: skip
def test_status_classification(status: int, retryable: bool) -> None:
    err = classify_response(_resp(status), "svc")
    assert err is not None
    assert err.retryable is retryable
    assert err.code == "provider_error"
    assert "nope" in err.message and str(status) in err.message


def test_success_is_not_an_error() -> None:
    assert classify_response(_resp(200), "svc") is None


def test_retry_after() -> None:
    err = classify_response(_resp(429, {"Retry-After": "7"}), "svc")
    assert err is not None and err.retry_after == 7.0
    err = classify_response(_resp(503, {"Retry-After": "2"}), "svc")
    assert err is not None and err.retry_after == 2.0
    err = classify_response(_resp(500, {"Retry-After": "2"}), "svc")
    assert err is not None and err.retry_after is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    assert parse_retry_after("garbage") is None


def test_transport_errors_are_retryable() -> None:
    req = httpx.Request("GET", "http://svc")
    assert classify_exception(httpx.ConnectError("refused", request=req), "svc", "http://svc").retryable
    assert classify_exception(httpx.ReadTimeout("slow", request=req), "svc", "http://svc").retryable


def test_unreachable_embedder_is_retryable(ctx: AppContext) -> None:
    pid = ctx.providers.create(
        "far", "embedder", "openai_compatible", {"base_url": "http://nowhere.invalid", "model": "m"}
    ).id
    with pytest.raises(ExecutionError) as exc:
        ctx.embeddings.embed(pid, "document", ["x"])
    assert exc.value.retryable


# ---- concurrency and other clients ----------------------------------------------------------------


def test_per_provider_concurrency_limit(ctx: AppContext, fakes: FakeServices) -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def slow(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    fakes.script("embedder", "/embeddings", [slow] * 8)
    pid = embedder(ctx, max_concurrency=2)
    inst = ctx.providers.instance(pid)
    threads = [threading.Thread(target=inst.embed, args=([f"x{i}"], "document")) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2


def test_reranker_client(ctx: AppContext, fakes: FakeServices) -> None:
    pid = ctx.providers.create("rr", "reranker", "http", {"base_url": RERANKER_URL, "model": "m"}).id
    out = ctx.providers.instance(pid).rerank("warranty months", ["bananas", "the warranty is 24 months", "months"], 3)
    assert [i for i, _ in out] == [1, 2, 0]
    fakes.script("reranker", "/rerank", [httpx.Response(200, json={"results": [{"index": 9, "relevance_score": 1}]})])
    with pytest.raises(ExecutionError):
        ctx.providers.instance(pid).rerank("q", ["a"], 1)


def test_chunker_and_parser_clients(ctx: AppContext, fakes: FakeServices) -> None:
    cid = ctx.providers.create("ch", "chunker", "http", {"base_url": CHUNKER_URL}).id
    chunker = ctx.providers.instance(cid)
    doc = {"schema_version": "1.0", "blocks": [{"id": f"b{i}", "type": "paragraph", "text": f"p{i}"} for i in range(3)]}
    chunks = chunker.chunk(doc, {"blocks_per_chunk": 2})
    assert [c["block_ids"] for c in chunks] == [["b0", "b1"], ["b2"]]
    pid = ctx.providers.create("pa", "parser", "http", {"base_url": PARSER_URL}).id
    parser = ctx.providers.instance(pid)
    ir = parser.parse(b"# Title\n\nBody", "a.md", "text/markdown", {"ocr": True})
    assert [b["type"] for b in ir["blocks"]] == ["heading", "paragraph"]
    ir = parser.parse(b"page one\fpage two", "a.pdf", "application/pdf", {})
    assert [b["page"] for b in ir["blocks"]] == [1, 2]
    fakes.script("parser", "/parse", [400])
    with pytest.raises(ExecutionError) as exc:
        parser.parse(b"x", "a.pdf", "application/pdf", {})
    assert not exc.value.retryable
