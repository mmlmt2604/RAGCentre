from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.pipeline.planner import create_pipeline
from ragcentre.pipeline.status import status
from ragcentre.queue.claims import claim_run
from ragcentre.testing.fakes import PARSER_URL, FakeServices

MD = b"# Guide\n\nIntro paragraph with warranty period of 24 months.\n\n## Setup\n\n- first step\n- second step\n"


def _add_providers(ctx: AppContext, http_parser: bool = False) -> None:
    ctx.providers.create("hash", "embedder", "builtin_hash", {})
    ctx.providers.create("store", "vector_store", "sqlite_vec", {})
    if http_parser:
        ctx.providers.create("docling", "parser", "http", {"base_url": PARSER_URL})


def _corpus(ctx: AppContext, name: str = "corpus") -> str:
    from ragcentre.hashing import new_id
    from ragcentre.timeutil import now_iso

    cid = new_id()
    ctx.db.execute(
        "INSERT INTO corpora (id, name, metadata_schema, created_at) VALUES (?, ?, '{}', ?)", (cid, name, now_iso())
    )
    return cid


def _insert_doc(ctx: AppContext, corpus_id: str, content: bytes, doc_id: str) -> None:
    from ragcentre.timeutil import now_iso

    sha = ctx.blobs.put(content)
    ctx.db.execute(
        "INSERT INTO documents (id, corpus_id, filename, mime_type, size_bytes, content_sha256, metadata, "
        "attributes, revision, status, created_at, updated_at) VALUES (?, ?, 'a.md', 'text/markdown', ?, ?, "
        "'{}', '{}', 1, 'active', ?, ?)",
        (doc_id, corpus_id, len(content), sha, now_iso(), now_iso()),
    )


def _upload(
    client: TestClient,
    corpus_id: str,
    content: bytes,
    filename: str = "a.md",
    mime: str = "text/markdown",
    external_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    r = client.post(
        f"/v1/corpora/{corpus_id}/documents",
        files={"file": (filename, content, mime)},
        data={"metadata": json.dumps(metadata or {}), "attributes": "{}", "external_id": external_id or ""},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _pipeline(http_parser: bool = False, **changes: Any) -> dict[str, Any]:
    parser = "docling" if http_parser else "builtin-text"
    cfg: dict[str, Any] = {
        "name": "p",
        "nodes": [
            {"id": "parse", "type": "parse", "routes": [{"mime": "text/*", "provider": parser}]},
            {
                "id": "chunk",
                "type": "chunk",
                "inputs": ["parse"],
                "strategy": "structure",
                "options": {"max_chars": 1200, "overlap_chars": 0, "include_heading_path": True},
            },
            {"id": "embed", "type": "embed", "inputs": ["chunk"], "provider": "hash"},
            {
                "id": "vec",
                "type": "index_vector",
                "inputs": ["embed"],
                "provider": "store",
                "options": {"metric": "cosine"},
            },
            {"id": "fts", "type": "index_fts", "inputs": ["chunk"], "options": {"tokenizer": "unicode61"}},
        ],
        "retrieval": {
            "sources": [{"id": "dense", "index": "vec", "top_k": 30}, {"id": "sparse", "index": "fts", "top_k": 30}],
            "fusion": {"method": "rrf", "k": 60},
            "top_k": 8,
        },
    }
    for key, value in changes.items():
        if "." in key:
            part, rest = key.split(".", 1)
            obj = cfg
            for seg in part.split("|"):
                obj = obj[int(seg)] if isinstance(obj, list) else obj[seg]
            if rest in obj:
                obj[rest] = value
        else:
            cfg[key] = value
    return cfg


def _wait_ready(ctx: AppContext, pipeline_id: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if status(ctx, pipeline_id).status == "ready":
            return
        time.sleep(0.1)
    st = status(ctx, pipeline_id)
    raise AssertionError(f"pipeline did not reach ready in {timeout}s: {st.model_dump()}")


def test_end_to_end_reaches_ready(ctx: AppContext, client: TestClient) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    _upload(client, corpus_id, MD)
    pid = create_pipeline(ctx, corpus_id, _pipeline())
    ctx.start_workers(3)
    try:
        _wait_ready(ctx, pid)
    finally:
        ctx.close_workers()
    st = status(ctx, pid)
    assert st.coverage == 1.0 and st.document_count == 1
    # Chunks and index rows exist.
    assert ctx.db.scalar("SELECT COUNT(*) FROM chunks") >= 1
    assert ctx.db.scalar("SELECT COUNT(*) FROM index_records") == ctx.db.scalar("SELECT COUNT(*) FROM chunks") * 2
    store = ctx.providers.store(ctx.providers.by_name("store").id)
    info = store.collection_info(ctx.db.scalar("SELECT physical_name FROM indexes WHERE kind = 'vector'"))
    assert info is not None and info.count == ctx.db.scalar("SELECT COUNT(*) FROM chunks")
    # Query parsed view through the API.
    doc_id = ctx.db.scalar("SELECT id FROM documents")
    parsed = client.get(f"/v1/documents/{doc_id}/parsed", params={"pipeline_id": pid})
    assert parsed.status_code == 200
    assert parsed.json()["blocks"][0]["type"] == "heading"
    chunks = client.get(f"/v1/documents/{doc_id}/chunks", params={"pipeline_id": pid})
    assert chunks.status_code == 200 and chunks.json()


def test_executors_idempotent(ctx: AppContext) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    from ragcentre.hashing import new_id

    doc_id = new_id()
    _insert_doc(ctx, corpus_id, MD, doc_id)
    from ragcentre.pipeline.executors import execute
    from ragcentre.queue.claims import complete, is_current

    pid = create_pipeline(ctx, corpus_id, _pipeline())
    resolved = json.loads(ctx.db.scalar("SELECT resolved FROM pipelines WHERE id = ?", (pid,)))["nodes"]
    for node in resolved:
        row = ctx.db.one("SELECT * FROM node_runs WHERE document_id = ? AND node_fp = ?", (doc_id, node["node_fp"]))
        claim = ctx.db.one("SELECT * FROM node_runs WHERE id = ?", (row["id"],))
        assert claim is not None
        run = claim_run(ctx, "w1")
        assert run is not None and run.id == claim["id"]
        stats1 = execute(ctx, run)
        assert is_current(ctx, run)
        complete(ctx, run, stats1)
        before = ctx.db.all(
            "SELECT id, text, context, page_start, page_end, heading_path, block_ids FROM chunks ORDER BY id"
        )
        stats2 = execute(ctx, run)
        assert complete(ctx, run, stats2)  # completion conditional check is still valid
        after = ctx.db.all(
            "SELECT id, text, context, page_start, page_end, heading_path, block_ids FROM chunks ORDER BY id"
        )
        assert [dict(r) for r in before] == [dict(r) for r in after]
    # DB state: one set of chunks and index records.
    assert ctx.db.scalar("SELECT COUNT(*) FROM chunks") == ctx.db.scalar("SELECT COUNT(*) FROM index_records WHERE index_id IN (SELECT id FROM indexes WHERE kind='fts')")  # fmt: skip


def test_lease_expiry_reclaims(ctx: AppContext) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    from ragcentre.hashing import new_id

    doc_id = new_id()
    _insert_doc(ctx, corpus_id, b"# T\n\ntext", doc_id)
    create_pipeline(ctx, corpus_id, _pipeline())
    first = claim_run(ctx, "w1")
    assert first is not None
    ctx.db.execute("UPDATE node_runs SET lease_expires_at = '2000-01-01T00:00:00.000Z' WHERE id = ?", (first.id,))
    second = claim_run(ctx, "w2")
    assert second is not None and second.id == first.id and second.attempt == first.attempt + 1
    from ragcentre.pipeline.executors import execute
    from ragcentre.queue.claims import complete

    stats = execute(ctx, second)
    assert complete(ctx, second, stats)
    assert ctx.db.scalar("SELECT status FROM node_runs WHERE id = ?", (second.id,)) == "succeeded"


def test_parser_retries_then_succeeds(
    ctx: AppContext, fakes: FakeServices, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_providers(ctx, http_parser=True)
    corpus_id = _corpus(ctx)
    fakes.script("parser", "/parse", [503, 503, 503])
    import ragcentre.queue.claims as claims

    monkeypatch.setattr(claims, "_backoff_seconds", lambda attempt, max_attempts, retry_after: 0.0)
    _upload(client, corpus_id, MD)
    pid = create_pipeline(ctx, corpus_id, _pipeline(http_parser=True))
    ctx.start_workers(2)
    try:
        _wait_ready(ctx, pid)
    finally:
        ctx.close_workers()
    parse_run = ctx.db.one(
        "SELECT r.* FROM node_runs r JOIN pipeline_nodes pn ON pn.node_fp = r.node_fp "
        "WHERE pn.pipeline_id = ? AND pn.node_id = 'parse'",
        (pid,),
    )
    assert parse_run["status"] == "succeeded" and parse_run["attempt"] == 4


def test_parser_400_fails_without_retry(ctx: AppContext, fakes: FakeServices, client: TestClient) -> None:
    _add_providers(ctx, http_parser=True)
    corpus_id = _corpus(ctx)
    fakes.script("parser", "/parse", [400])
    _upload(client, corpus_id, MD)
    pid = create_pipeline(ctx, corpus_id, _pipeline(http_parser=True))
    ctx.start_workers(2)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and status(ctx, pid).status == "building":
            time.sleep(0.1)
        st = status(ctx, pid)
        assert st.status == "degraded"
    finally:
        ctx.close_workers()
    parse_run = ctx.db.one(
        "SELECT r.* FROM node_runs r JOIN pipeline_nodes pn ON pn.node_fp = r.node_fp "
        "WHERE pn.pipeline_id = ? AND pn.node_id = 'parse'",
        (pid,),
    )
    assert parse_run["status"] == "failed" and parse_run["attempt"] == 1
    assert parse_run["error_code"] == "provider_error"


def test_downstream_blocked_and_retry_failed_unblocks(ctx: AppContext, fakes: FakeServices, client: TestClient) -> None:
    _add_providers(ctx, http_parser=True)
    corpus_id = _corpus(ctx)
    fakes.script("parser", "/parse", [400])
    _upload(client, corpus_id, MD)
    pid = create_pipeline(ctx, corpus_id, _pipeline(http_parser=True))
    ctx.start_workers(2)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and status(ctx, pid).status == "building":
            time.sleep(0.1)
        st = status(ctx, pid)
        assert st.status == "degraded"
        chunk = next(n for n in st.nodes if n.node_id == "chunk")
        assert chunk.counts.blocked == 1
        # Retry the failed parse run through the pipeline endpoint.
        r = client.post(f"/v1/pipelines/{pid}/retry-failed")
        assert r.json()["retried"] == 1
        fakes.script("parser", "/parse", [])
        _wait_ready(ctx, pid)
    finally:
        ctx.close_workers()


def test_replace_content_removes_stale_records(ctx: AppContext, fakes: FakeServices, client: TestClient) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    _upload(client, corpus_id, MD, external_id="e1")
    pid = create_pipeline(ctx, corpus_id, _pipeline())
    ctx.start_workers(2)
    try:
        _wait_ready(ctx, pid)
        store = ctx.providers.store(ctx.providers.by_name("store").id)
        physical = ctx.db.scalar("SELECT physical_name FROM indexes WHERE kind = 'vector'")
        info = store.collection_info(physical)
        old_count = info.count if info else 0
        # Replace with a shorter file that produces fewer chunks.
        r = client.post(
            f"/v1/corpora/{corpus_id}/documents",
            files={"file": ("short.md", b"# T\n\nOne paragraph.", "text/markdown")},
            data={"metadata": "{}", "attributes": "{}", "external_id": "e1"},
        )
        assert r.status_code == 200, r.text
        _wait_ready(ctx, pid)
        info2 = store.collection_info(physical)
        assert info2 is not None and info2.count <= old_count
        assert ctx.db.scalar("SELECT COUNT(*) FROM chunks") == info2.count
        fts_count = ctx.db.scalar(
            "SELECT COUNT(*) FROM index_records WHERE index_id IN (SELECT id FROM indexes WHERE kind='fts')"
        )
        assert fts_count == info2.count
    finally:
        ctx.close_workers()


def test_delete_document_removes_everywhere(ctx: AppContext, client: TestClient) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    doc = _upload(client, corpus_id, MD)
    pid = create_pipeline(ctx, corpus_id, _pipeline())
    ctx.start_workers(2)
    try:
        _wait_ready(ctx, pid)
        store = ctx.providers.store(ctx.providers.by_name("store").id)
        physical = ctx.db.scalar("SELECT physical_name FROM indexes WHERE kind = 'vector'")
        assert store.collection_info(physical).count >= 1  # type: ignore[union-attr]
        r = client.delete(f"/v1/documents/{doc['id']}")
        assert r.status_code == 202
        # Run the delete task synchronously.
        from ragcentre.queue.tasks import run_task

        task = ctx.db.one("SELECT * FROM tasks WHERE kind = 'delete_document'")
        assert task is not None
        run_task(ctx, task)
        assert store.collection_info(physical).count == 0  # type: ignore[union-attr]
        assert ctx.db.scalar("SELECT COUNT(*) FROM chunks") == 0
        assert ctx.db.scalar("SELECT COUNT(*) FROM index_records") == 0
        assert ctx.db.scalar("SELECT COUNT(*) FROM documents WHERE status = 'active'") == 0
    finally:
        ctx.close_workers()


def test_superseded_revision_discarded(ctx: AppContext) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    from ragcentre.hashing import new_id

    doc_id = new_id()
    _insert_doc(ctx, corpus_id, b"# T\n\ntext", doc_id)
    create_pipeline(ctx, corpus_id, _pipeline())
    first = claim_run(ctx, "w1")
    assert first is not None
    from ragcentre.pipeline.planner import on_document_replaced

    with ctx.db.write() as conn:
        on_document_replaced(conn, corpus_id, doc_id)
    from ragcentre.pipeline.executors import execute
    from ragcentre.queue.claims import complete

    stats = execute(ctx, first)
    assert not complete(ctx, first, stats)  # stale result discarded
    assert ctx.db.scalar("SELECT status FROM node_runs WHERE id = ?", (first.id,)) != "succeeded"


def test_second_pipeline_changing_embedder_makes_zero_parser_calls(
    ctx: AppContext, fakes: FakeServices, client: TestClient
) -> None:
    _add_providers(ctx, http_parser=True)
    ctx.providers.create("hash2", "embedder", "builtin_hash", {"dimensions": 128})
    corpus_id = _corpus(ctx)
    _upload(client, corpus_id, MD)
    pid1 = create_pipeline(ctx, corpus_id, _pipeline(http_parser=True))
    ctx.start_workers(2)
    try:
        _wait_ready(ctx, pid1)
        parser_calls = fakes.count("parser")
        assert parser_calls > 0
        cfg2 = _pipeline(http_parser=True)
        cfg2["name"] = "p2"
        cfg2["nodes"][2]["provider"] = "hash2"
        pid2 = create_pipeline(ctx, corpus_id, cfg2)
        _wait_ready(ctx, pid2)
        assert fakes.count("parser") == parser_calls
    finally:
        ctx.close_workers()
