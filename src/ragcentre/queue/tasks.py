"""Maintenance tasks outside the node graph (section 12.5)."""

from __future__ import annotations

import json
from typing import Any

from ragcentre.errors import ExecutionError
from ragcentre.locks import document_lock, index_catalog_lock
from ragcentre.logs import redact
from ragcentre.pipeline.purge import purge_blobs, release_document
from ragcentre.stores.base import delete_batched
from ragcentre.timeutil import after, now_iso


def _run_delete_document(ctx: Any, payload: dict[str, Any]) -> None:
    document_id = payload["document_id"]
    doc = ctx.db.one("SELECT * FROM documents WHERE id = ? AND status = 'deleting'", (document_id,))
    if doc is None:
        return
    rows = ctx.db.all(
        "SELECT DISTINCT i.* FROM indexes i JOIN index_records ir ON ir.index_id = i.id WHERE ir.document_id = ?",
        (document_id,),
    )
    for index in rows:
        if index["kind"] != "fts":
            record_ids = [
                r["record_id"]
                for r in ctx.db.all(
                    "SELECT record_id FROM index_records WHERE index_id = ? AND document_id = ?",
                    (index["id"], document_id),
                )
            ]
            if record_ids:
                with ctx.providers.lease(index["provider_id"]) as store:
                    delete_batched(store, index["physical_name"], record_ids)
    with ctx.db.write() as conn:
        blobs = release_document(conn, doc)
        conn.execute("DELETE FROM index_records WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM node_runs WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    purge_blobs(ctx, blobs)


def _run_drop_index(ctx: Any, payload: dict[str, Any]) -> None:
    index_id = payload["index_id"]
    with index_catalog_lock(ctx.settings.data_dir):
        with ctx.db.read() as conn:
            index = conn.execute("SELECT * FROM indexes WHERE id = ?", (index_id,)).fetchone()
            if index is None:
                return
            live = conn.execute(
                "SELECT 1 FROM pipelines p JOIN pipeline_nodes pn ON pn.pipeline_id = p.id "
                "WHERE p.corpus_id = ? AND pn.node_fp = ? AND p.archived = 0 LIMIT 1",
                (index["corpus_id"], index["node_fp"]),
            ).fetchone()
            if live is not None:
                return
            shared = conn.execute(
                "SELECT 1 FROM indexes WHERE kind = ? AND physical_name = ? AND id != ? LIMIT 1",
                (index["kind"], index["physical_name"], index_id),
            ).fetchone()
        if shared is None and index["kind"] == "vector":
            # The catalog lock keeps the reference check valid without holding a SQLite write
            # transaction while waiting for a remote store.
            with ctx.providers.lease(index["provider_id"]) as store:
                store.drop_collection(index["physical_name"])
        with ctx.db.write() as conn:
            if shared is None and index["kind"] == "fts":
                table = f"fts_{index['physical_name']}"
                conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            conn.execute("DELETE FROM index_records WHERE index_id = ?", (index_id,))
            conn.execute("DELETE FROM indexes WHERE id = ?", (index_id,))


def _run_eval(ctx: Any, payload: dict[str, Any]) -> None:
    from ragcentre.eval.runner import run_eval

    run_eval(ctx, payload["run_id"])


def on_task_failed(ctx: Any, task: Any) -> None:
    """Called when a task has failed permanently."""
    payload = json.loads(task["payload"])
    if task["kind"] == "eval_run":
        from ragcentre.eval.runner import mark_failed

        mark_failed(ctx, payload["run_id"])
    elif task["kind"] == "sync_source":
        source_id = payload["source_id"]
        row = ctx.db.one("SELECT schedule_seconds FROM sync_sources WHERE id = ?", (source_id,))
        if row is not None:
            schedule = int(row["schedule_seconds"])
            ctx.db.execute(
                "UPDATE sync_sources SET status = 'error', last_error = ?, last_run_at = ?, next_run_at = ? "
                "WHERE id = ?",
                (redact(task["error_message"])[:2000] if task["error_message"] else None,
                 now_iso(), after(schedule) if schedule else None, source_id),
            )


def run_task(ctx: Any, task: Any) -> None:
    kind = task["kind"]
    payload = json.loads(task["payload"])
    if kind == "delete_document":
        with document_lock(ctx.settings.data_dir, payload["document_id"]):
            _run_delete_document(ctx, payload)
    elif kind == "purge_blobs":
        purge_blobs(ctx, payload["blobs"])
    elif kind == "drop_index":
        _run_drop_index(ctx, payload)
    elif kind == "eval_run":
        _run_eval(ctx, payload)
    elif kind == "sync_source":
        from ragcentre.connectors.sync import run_sync_source

        run_sync_source(ctx, payload["source_id"])
    else:
        raise ExecutionError("unknown_task", f"unknown task kind {kind}", retryable=False)
