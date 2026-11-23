"""Production deletion respects a store's request batch limit and keeps index tracking consistent."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from ragcentre.context import AppContext
from ragcentre.hashing import new_id
from ragcentre.pipeline.executors import execute
from ragcentre.pipeline.planner import create_pipeline
from ragcentre.queue.claims import ClaimedRun, claim_run, complete
from ragcentre.queue.tasks import run_task
from ragcentre.stores.base import Record, VectorStore, upsert_batched
from test_m6_ingestion import MD, _add_providers, _corpus, _insert_doc, _pipeline


def _indexed_document(ctx: AppContext) -> tuple[str, ClaimedRun, VectorStore, dict[str, Any]]:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    document_id = new_id()
    _insert_doc(ctx, corpus_id, MD, document_id)
    config = _pipeline()
    config["nodes"] = [node for node in config["nodes"] if node["id"] != "fts"]
    config["retrieval"]["sources"] = [config["retrieval"]["sources"][0]]
    config["retrieval"].pop("fusion")
    create_pipeline(ctx, corpus_id, config)
    for _ in range(3):
        claim = claim_run(ctx, "batch-test")
        assert claim is not None
        assert complete(ctx, claim, execute(ctx, claim))
    claim = claim_run(ctx, "batch-test")
    assert claim is not None and claim.node_type == "index_vector"
    execute(ctx, claim)
    index = ctx.db.one("SELECT * FROM indexes WHERE kind = 'vector'")
    assert index is not None
    store = ctx.providers.store(index["provider_id"])
    return document_id, claim, store, dict(index)


def _add_old_records(ctx: AppContext, document_id: str, store: VectorStore, index: dict[str, Any]) -> set[str]:
    info = store.collection_info(index["physical_name"])
    assert info is not None
    records = [Record(new_id(), [1.0] * info.dim, {"document_id": document_id}) for _ in range(5)]
    upsert_batched(store, index["physical_name"], records)
    with ctx.db.write() as conn:
        conn.executemany(
            "INSERT INTO index_records (index_id, document_id, record_id) VALUES (?, ?, ?)",
            [(index["id"], document_id, record.id) for record in records],
        )
    return {record.id for record in records}


def _limit_deletes(monkeypatch: pytest.MonkeyPatch, store: VectorStore) -> list[list[str]]:
    monkeypatch.setattr(store, "capabilities", replace(store.capabilities, max_batch=2))
    delete = store.delete
    calls: list[list[str]] = []

    def limited(name: str, ids: Sequence[str]) -> None:
        assert 0 < len(ids) <= store.capabilities.max_batch, "delete exceeded the store's batch limit"
        calls.append(list(ids))
        delete(name, ids)

    monkeypatch.setattr(store, "delete", limited)
    return calls


def test_reindex_deletes_stale_records_in_batches(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    document_id, claim, store, index = _indexed_document(ctx)
    stale_ids = _add_old_records(ctx, document_id, store, index)
    calls = _limit_deletes(monkeypatch, store)

    stats = execute(ctx, claim)

    assert sorted(map(len, calls)) == [1, 2, 2]
    assert {record_id for batch in calls for record_id in batch} == stale_ids
    info = store.collection_info(index["physical_name"])
    assert info is not None and info.count == stats["n_records"]
    tracked_ids = {
        row["record_id"] for row in ctx.db.all("SELECT record_id FROM index_records WHERE index_id = ?", (index["id"],))
    }
    assert len(tracked_ids) == stats["n_records"]
    assert not tracked_ids & stale_ids
    assert {match.id for match in store.query(index["physical_name"], [1.0] * info.dim, 100)} == tracked_ids


def test_document_deletion_batches_all_index_records(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    document_id, claim, store, index = _indexed_document(ctx)
    assert complete(ctx, claim, {})
    _add_old_records(ctx, document_id, store, index)
    expected_ids = {
        row["record_id"]
        for row in ctx.db.all("SELECT record_id FROM index_records WHERE document_id = ?", (document_id,))
    }
    assert len(expected_ids) > 2
    calls = _limit_deletes(monkeypatch, store)

    ctx.db.execute("UPDATE documents SET status = 'deleting' WHERE id = ?", (document_id,))
    run_task(ctx, {"kind": "delete_document", "payload": json.dumps({"document_id": document_id})})

    assert len(calls) == (len(expected_ids) + 1) // 2
    assert {record_id for batch in calls for record_id in batch} == expected_ids
    info = store.collection_info(index["physical_name"])
    assert info is not None and info.count == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM index_records WHERE document_id = ?", (document_id,)) == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM chunks WHERE document_id = ?", (document_id,)) == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM documents WHERE id = ?", (document_id,)) == 0
