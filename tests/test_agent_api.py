"""Agent passage budgets, bounded reading and lifecycle/stage isolation."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.models.api import QueryResponse
from ragcentre.pipeline.planner import create_pipeline
from ragcentre.retrieval.budget import budget_response, passage_chars
from test_m6_ingestion import _corpus, _upload
from test_m7_retrieval import drain


def config(name: str = "agent", size: int = 500) -> dict[str, Any]:
    return {
        "name": name,
        "nodes": [
            {"id": "parse", "type": "parse", "routes": [{"mime": "text/*", "provider": "builtin-text"}]},
            {
                "id": "chunk",
                "type": "chunk",
                "inputs": ["parse"],
                "strategy": "fixed",
                "options": {"max_chars": size, "overlap_chars": 0},
            },
            {"id": "fts", "type": "index_fts", "inputs": ["chunk"]},
        ],
        "retrieval": {
            "sources": [{"id": "keyword", "index": "fts", "top_k": 20}],
            "top_k": 10,
            "expand": {"neighbors": 2},
        },
    }


@pytest.fixture
def indexed(ctx: AppContext, client: TestClient) -> tuple[str, str, str, list[dict[str, Any]]]:
    cid = _corpus(ctx)
    did = _upload(client, cid, ("warranty 自転車 🚲 " * 200).encode())["id"]
    pid = create_pipeline(ctx, cid, config())
    drain(ctx)
    ctx.db.execute("UPDATE corpora SET active_pipeline_id = ? WHERE id = ?", (pid, cid))
    chunks = client.get(f"/v1/documents/{did}/chunks", params={"pipeline_id": pid}).json()
    assert len(chunks) > 5
    return cid, did, pid, chunks


def response(texts: list[str], **supplemental: Any) -> QueryResponse:
    return QueryResponse.model_validate(
        {
            "pipeline_id": "p",
            "pipeline_fingerprint": "fp",
            "partial": False,
            "enriched_sources": [],
            "timings_ms": {"total": 1},
            "transform": None,
            "results": [
                {
                    "chunk_id": f"k{i}",
                    "document_id": "d",
                    "ordinal": i,
                    "rank": i + 1,
                    "score": 1 / (i + 1),
                    "text": text,
                    "context": None,
                    "enrichment": None,
                    "page_start": 1,
                    "page_end": 1,
                    "heading_path": [],
                    "document": {"filename": "a", "external_id": None, "metadata": {}, "attributes": {}},
                    "context_before": [],
                    "context_after": [],
                    "sources": {"s": {"rank": i + 1, "score": 1}},
                    "probes": [0],
                    **supplemental,
                }
                for i, text in enumerate(texts)
            ],
        }
    )


@pytest.mark.parametrize("texts", [[], ["é🚲" * 100], ["a" * 100, "b" * 100], ["a" * 201], ["a" * 150] * 3])
def test_budget_boundaries_and_no_mutation(texts: list[str]) -> None:
    original = response(texts)
    snapshot = original.model_dump()
    unlimited = budget_response(original, None)
    assert unlimited is original and unlimited.truncated is False
    shaped = budget_response(original, 200)
    assert sum(passage_chars(h) for h in shaped.results) <= 200
    assert shaped.truncated is (sum(map(len, texts)) > 200)
    assert original.model_dump() == snapshot
    for hit, old in zip(shaped.results, original.results, strict=False):
        assert hit.model_dump(exclude={"text"}) == old.model_dump(exclude={"text"})
    if texts and len(texts[0]) > 200:
        assert shaped.results[0].text == texts[0][:200]


def test_budget_removes_all_supplements_before_dropping_hits() -> None:
    original = response(
        ["a" * 100, "b" * 100],
        context="界" * 90,
        enrichment="é" * 90,
        context_before=["🚲" * 80],
        context_after=["x" * 80],
    )
    shaped = budget_response(original, 200)
    assert [r.text for r in shaped.results] == [r.text for r in original.results]
    assert all(
        r.context is None and r.enrichment is None and r.context_before == r.context_after == [] for r in shaped.results
    )
    assert shaped.truncated and sum(passage_chars(r) for r in shaped.results) == 200
    # Repeated content counts every time it is transmitted, including repeats within one result.
    repeated = response(["a" * 100], context="a" * 100, context_before=["a" * 100])
    assert passage_chars(repeated.results[0]) == 300
    assert budget_response(repeated, 200).truncated


@pytest.mark.parametrize("limit", [199, -1, 0])
def test_query_rejects_invalid_budget(client: TestClient, limit: int) -> None:
    r = client.post("/v1/corpora/missing/query", json={"query": "warranty", "max_chars": limit})
    assert r.status_code == 422


def test_query_budget_and_ordinals_preserve_ranking(client: TestClient, indexed: Any) -> None:
    cid, _, _, chunks = indexed
    path = f"/v1/corpora/{cid}/query"
    plain = client.post(path, json={"query": "warranty", "debug": True}).json()
    assert plain["results"] and plain["truncated"] is False
    exact = sum(passage_chars(r) for r in QueryResponse.model_validate(plain).results)
    fitted = client.post(path, json={"query": "warranty", "debug": True, "max_chars": exact}).json()
    assert fitted["results"] == plain["results"] and fitted["truncated"] is False
    for transform in (None, False, True):
        bounded = client.post(
            path, json={"query": "warranty", "debug": True, "max_chars": 200, "transform": transform}
        ).json()
        assert bounded["truncated"] and len(bounded["results"]) == 1
        assert sum(passage_chars(r) for r in QueryResponse.model_validate(bounded).results) <= 200
        first = bounded["results"][0]
        for field in ("chunk_id", "document_id", "ordinal", "score", "rank", "sources", "probes"):
            assert first[field] == plain["results"][0][field]
    assert {c["id"]: c["ordinal"] for c in chunks} == {r["chunk_id"]: r["ordinal"] for r in plain["results"]}
    assert client.post(path, json={"query": "warranty"}).json()["results"] == plain["results"]


@pytest.mark.parametrize("position", [0, 3, -1])
@pytest.mark.parametrize("width", [0, 2, 10])
def test_chunk_window_edges(client: TestClient, indexed: Any, position: int, width: int) -> None:
    cid, did, _, chunks = indexed
    pos = position % len(chunks)
    r = client.get(f"/v1/chunks/{chunks[pos]['id']}", params={"before": width, "after": width})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["chunk"] == chunks[pos]
    assert out["before"] == chunks[max(0, pos - width) : pos]
    assert out["after"] == chunks[pos + 1 : pos + width + 1]
    assert out["document"]["id"] == did and out["document"]["corpus_id"] == cid


@pytest.mark.parametrize("params", [{"before": -1}, {"after": 11}, {"before": "bad"}])
def test_window_limit_validation(client: TestClient, params: Any) -> None:
    assert client.get("/v1/chunks/missing", params=params).status_code == 422


def test_window_stage_document_and_revision_isolation(ctx: AppContext, client: TestClient, indexed: Any) -> None:
    cid, did, _, chunks = indexed
    other_doc = _upload(client, cid, b"warranty other document")["id"]
    other_pid = create_pipeline(ctx, cid, config("other-stage", size=600))
    clone = create_pipeline(ctx, cid, config("clone"))
    drain(ctx)
    assert client.get(f"/v1/documents/{did}/chunks", params={"pipeline_id": clone}).json() == chunks
    foreign = client.get(f"/v1/documents/{other_doc}/chunks", params={"pipeline_id": other_pid}).json()
    assert foreign
    ctx.db.execute("UPDATE chunks SET doc_revision = 0 WHERE id = ?", (chunks[1]["id"],))
    out = client.get(f"/v1/chunks/{chunks[2]['id']}", params={"before": 10, "after": 10}).json()
    ids = [c["id"] for c in out["before"] + [out["chunk"]] + out["after"]]
    assert ids == [c["id"] for c in chunks if c["id"] != chunks[1]["id"]]
    assert client.get(f"/v1/chunks/{chunks[1]['id']}").status_code == 404
    assert client.get("/v1/chunks/unknown").status_code == 404
    # Inactive stages are retained and individually readable, with their own neighbours.
    other_chunks = client.get(f"/v1/documents/{did}/chunks", params={"pipeline_id": other_pid}).json()
    assert client.get(f"/v1/chunks/{other_chunks[0]['id']}").status_code == 200
    assert client.delete(f"/v1/documents/{did}").status_code == 202
    assert client.get(f"/v1/chunks/{chunks[0]['id']}").status_code == 404
    assert client.get(f"/v1/documents/{did}/outline").status_code == 404


def set_chunks(ctx: AppContext, indexed: Any, headings: list[list[str]], pages: bool = True) -> list[str]:
    _, did, _, chunks = indexed
    fp = ctx.db.scalar("SELECT chunk_node_fp FROM chunks WHERE id = ?", (chunks[0]["id"],))
    ctx.db.execute("DELETE FROM chunks WHERE document_id = ?", (did,))
    ids = [f"outline-{i}" for i in range(len(headings))]
    with ctx.db.write() as conn:
        conn.executemany(
            "INSERT INTO chunks (id, chunk_node_fp, document_id, ordinal, text, context, page_start, page_end, "
            "heading_path, block_ids, metadata, doc_revision) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, '[]', '{}', 1)",
            [
                (
                    ids[i],
                    fp,
                    did,
                    i,
                    "DO NOT RETURN BODY" * 100,
                    i // 10 + 1 if pages else None,
                    i // 10 + 1 if pages else None,
                    json.dumps(h),
                )
                for i, h in enumerate(headings)
            ],
        )
    return ids


def test_outline_consecutive_headings_and_parity(ctx: AppContext, client: TestClient, indexed: Any) -> None:
    _, did, pid, _ = indexed
    headings = [["A"], ["A"], ["A", "B"], ["C"], ["A"]]
    ids = set_chunks(ctx, indexed, headings)
    r = client.get(f"/v1/documents/{did}/outline")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["chunk_count"] == 5 and out["page_count"] is None and out["truncated"] is False
    assert [s["heading_path"] for s in out["sections"]] == [["A"], ["A", "B"], ["C"], ["A"]]
    assert [s["chunk_count"] for s in out["sections"]] == [2, 1, 1, 1]
    assert [s["first_chunk_id"] for s in out["sections"]] == [ids[0], ids[2], ids[3], ids[4]]
    assert client.get(f"/v1/documents/{did}/outline", params={"pipeline_id": pid}).json() == out
    assert "DO NOT RETURN BODY" not in r.text and '"text"' not in r.text
    assert [
        c["heading_path"] for c in client.get(f"/v1/documents/{did}/chunks", params={"pipeline_id": pid}).json()
    ] == headings


@pytest.mark.parametrize("pages", [True, False])
def test_headingless_outline_has_ten_chunk_entry_points(
    ctx: AppContext, client: TestClient, indexed: Any, pages: bool
) -> None:
    _, did, _, _ = indexed
    ids = set_chunks(ctx, indexed, [[]] * 25, pages=pages)
    out = client.get(f"/v1/documents/{did}/outline").json()
    assert [s["chunk_count"] for s in out["sections"]] == [10, 10, 5]
    assert [s["first_chunk_id"] for s in out["sections"]] == ids[::10]
    assert out["page_count"] is None  # Chunk ranges cannot account for blank/trailing source pages.
    assert [s["page_start"] for s in out["sections"]] == ([1, 2, 3] if pages else [None] * 3)


@pytest.mark.parametrize("count", [199, 200, 201])
def test_outline_cap_keeps_full_count(ctx: AppContext, client: TestClient, indexed: Any, count: int) -> None:
    _, did, _, _ = indexed
    set_chunks(ctx, indexed, [[f"Section {i}"] for i in range(count)])
    out = client.get(f"/v1/documents/{did}/outline").json()
    assert out["chunk_count"] == count and len(out["sections"]) == min(count, 200)
    assert out["truncated"] is (count > 200)


def test_outline_pending_selection_and_freshness(ctx: AppContext, client: TestClient, indexed: Any) -> None:
    cid, did, pid, chunks = indexed
    path = f"/v1/documents/{did}/outline"
    other = _corpus(ctx, "foreign")
    foreign = create_pipeline(ctx, other, config("foreign"))
    assert client.get(path, params={"pipeline_id": foreign}).status_code == 404
    ctx.db.execute("UPDATE corpora SET active_pipeline_id = NULL WHERE id = ?", (cid,))
    assert client.get(path).status_code == 404
    assert client.get(path, params={"pipeline_id": pid}).status_code == 200
    ctx.db.execute("UPDATE corpora SET active_pipeline_id = ? WHERE id = ?", (pid, cid))
    ctx.db.execute("UPDATE documents SET revision = revision + 1 WHERE id = ?", (did,))
    out = client.get(path).json()
    assert out["chunk_count"] == 0 and out["sections"] == [] and out["truncated"] is False
    assert client.get(f"/v1/chunks/{chunks[0]['id']}").status_code == 404
    assert client.get("/v1/documents/missing/outline").status_code == 404


def test_outline_uses_same_stage_selection_as_chunks(ctx: AppContext, client: TestClient, indexed: Any) -> None:
    _, did, pid, chunks = indexed
    resolved = json.loads(ctx.db.scalar("SELECT resolved FROM pipelines WHERE id = ?", (pid,)))
    chunk = next(n for n in resolved["nodes"] if n["type"] == "chunk")
    resolved["nodes"].append({**chunk, "node_id": "ambiguous", "node_fp": "other"})
    ctx.db.execute("UPDATE pipelines SET resolved = ? WHERE id = ?", (json.dumps(resolved), pid))
    assert client.get(f"/v1/documents/{did}/outline").status_code == 422
    assert client.get(f"/v1/documents/{did}/chunks", params={"pipeline_id": pid}).status_code == 422
    resolved["nodes"].append(
        {"type": "enrich", "node_id": "enrich", "node_fp": "enrich-fp", "runtime": {"chunk_fp": chunk["node_fp"]}}
    )
    resolved["retrieval_runtime"]["enrich"] = {"node_id": "enrich"}
    ctx.db.execute("UPDATE pipelines SET resolved = ? WHERE id = ?", (json.dumps(resolved), pid))
    out = client.get(f"/v1/documents/{did}/outline").json()
    assert out["chunk_count"] == len(chunks) and out["sections"][0]["first_chunk_id"] == chunks[0]["id"]
    assert len(client.get(f"/v1/documents/{did}/chunks", params={"pipeline_id": pid}).json()) == len(chunks)


@pytest.mark.parametrize("mutation", ["delete", "replace"])
@pytest.mark.parametrize("resource", ["chunk", "outline"])
def test_fetch_rechecks_after_lock_wait(
    ctx: AppContext, client: TestClient, indexed: Any, monkeypatch: pytest.MonkeyPatch, mutation: str, resource: str
) -> None:
    from ragcentre.api.routers import documents

    _, did, _, chunks = indexed
    reached, resume = threading.Event(), threading.Event()
    original = documents.document_lock

    @contextmanager
    def controlled_lock(data_dir: Any, document_id: str) -> Any:
        if threading.current_thread().name.startswith("fetch"):
            reached.set()
            assert resume.wait(5)
        with original(data_dir, document_id):
            yield

    monkeypatch.setattr(documents, "document_lock", controlled_lock)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="fetch") as pool:
        # Direct route execution keeps the test's barrier on the thread making the read.
        future = (
            pool.submit(documents.get_chunk, chunks[0]["id"], 2, 2, ctx)
            if resource == "chunk"
            else pool.submit(documents.get_outline, did, None, ctx)
        )
        try:
            assert reached.wait(5)
            if mutation == "delete":
                assert client.delete(f"/v1/documents/{did}").status_code == 202
            else:
                assert (
                    client.put(f"/v1/documents/{did}/content", files={"file": ("new.md", b"new text")}).status_code
                    == 200
                )
        finally:
            resume.set()
        from ragcentre.errors import NotFound

        if resource == "outline" and mutation == "replace":
            out = future.result(timeout=5)
            assert out.chunk_count == 0 and out.sections == [] and out.document.revision == 2
        else:
            with pytest.raises(NotFound):
                future.result(timeout=5)


@pytest.mark.parametrize("credential", ["anonymous", "invalid", "read", "full", "session"])
def test_populated_fetch_permissions(ctx: AppContext, client: TestClient, indexed: Any, credential: str) -> None:
    _, did, _, chunks = indexed
    ctx.accounts.create_account("agent-user", "", "agent-test-password")
    if credential == "session":
        assert (
            client.post(
                "/v1/auth/login", json={"username": "agent-user", "password": "agent-test-password"}
            ).status_code
            == 200
        )
    headers = {}
    if credential in ("read", "full"):
        headers = {"Authorization": f"Bearer {ctx.accounts.create_key('agent-' + credential, credential)['key']}"}
    elif credential == "invalid":
        headers = {"Authorization": "Bearer invalid"}
    expected = 401 if credential in ("anonymous", "invalid") else 200
    for path in (f"/v1/chunks/{chunks[0]['id']}", f"/v1/documents/{did}/outline"):
        r = client.get(path, headers=headers)
        assert r.status_code == expected, r.text
        if expected == 200:
            assert r.json()["document"]["id"] == did
