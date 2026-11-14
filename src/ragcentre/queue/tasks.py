"""Maintenance tasks outside the node graph (section 12.5)."""

from __future__ import annotations

import json
from typing import Any

from ragcentre.errors import ExecutionError


def _run_delete_document(ctx: Any, payload: dict[str, Any]) -> None:
    document_id = payload["document_id"]
    rows = ctx.db.all(
        "SELECT DISTINCT i.* FROM indexes i JOIN index_records ir ON ir.index_id = i.id WHERE ir.document_id = ?",
        (document_id,),
    )
    for index in rows:
        if index["kind"] == "fts":
            table = f"fts_{index['physical_name']}"
            ctx.db.execute(f'DELETE FROM "{table}" WHERE document_id = ?', (document_id,))
        else:
            store = ctx.providers.store(index["provider_id"])
            record_ids = [
                r["record_id"]
                for r in ctx.db.all(
                    "SELECT record_id FROM index_records WHERE index_id = ? AND document_id = ?",
                    (index["id"], document_id),
                )
            ]
            if record_ids:
                store.delete(index["physical_name"], record_ids)
    with ctx.db.write() as conn:
        conn.execute("DELETE FROM index_records WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM node_runs WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))


def _run_drop_index(ctx: Any, payload: dict[str, Any]) -> None:
    index_id = payload["index_id"]
    index = ctx.db.one("SELECT * FROM indexes WHERE id = ?", (index_id,))
    if index is None:
        return
    if index["kind"] == "fts":
        table = f"fts_{index['physical_name']}"
        ctx.db.execute(f'DROP TABLE IF EXISTS "{table}"')
    else:
        store = ctx.providers.store(index["provider_id"])
        store.drop_collection(index["physical_name"])
    with ctx.db.write() as conn:
        conn.execute("DELETE FROM index_records WHERE index_id = ?", (index_id,))
        conn.execute("DELETE FROM indexes WHERE id = ?", (index_id,))


def _run_eval(ctx: Any, payload: dict[str, Any]) -> None:
    from ragcentre.eval.runner import run_eval

    run_eval(ctx, payload["run_id"])


def on_task_failed(ctx: Any, task: Any) -> None:
    """Called when a task has failed permanently."""
    if task["kind"] == "eval_run":
        from ragcentre.eval.runner import mark_failed

        mark_failed(ctx, json.loads(task["payload"])["run_id"])


def run_task(ctx: Any, task: Any) -> None:
    kind = task["kind"]
    payload = json.loads(task["payload"])
    if kind == "delete_document":
        from ragcentre.locks import document_lock

        with document_lock(ctx.settings.data_dir, payload["document_id"]):
            _run_delete_document(ctx, payload)
    elif kind == "drop_index":
        _run_drop_index(ctx, payload)
    elif kind == "eval_run":
        _run_eval(ctx, payload)
    else:
        raise ExecutionError("unknown_task", f"unknown task kind {kind}", retryable=False)
