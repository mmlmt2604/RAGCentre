"""Remove retained data after document deletion or replacement."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ragcentre.errors import ExecutionError
from ragcentre.hashing import parse_cache_key
from ragcentre.pipeline.planner import enqueue_task

if TYPE_CHECKING:
    from ragcentre.context import AppContext


def scrub_retrieved(entry: dict[str, Any]) -> dict[str, Any]:
    return {**entry, "text": "", "document_label": "(removed)", "page_start": None, "page_end": None}


def release_document(conn: sqlite3.Connection, doc: sqlite3.Row | Mapping[str, Any]) -> list[str]:
    """Detach retained data in the same transaction that removes or replaces the document."""
    document_id, content = doc["id"], doc["content_sha256"]
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM chunk_enrichments WHERE document_id = ?", (document_id,))
    rows = conn.execute(
        "SELECT run_id, pipeline_id, item_id, retrieved FROM eval_results "
        "WHERE EXISTS (SELECT 1 FROM json_each(retrieved) WHERE json_extract(value, '$.document_id') = ?)",
        (document_id,),
    ).fetchall()
    for row in rows:
        entries = json.loads(row["retrieved"])
        entries = [scrub_retrieved(e) if e["document_id"] == document_id else e for e in entries]
        conn.execute(
            "UPDATE eval_results SET retrieved = ? WHERE run_id = ? AND pipeline_id = ? AND item_id = ?",
            (json.dumps(entries), row["run_id"], row["pipeline_id"], row["item_id"]),
        )
    for row in conn.execute(
        "SELECT DISTINCT i.physical_name FROM indexes i JOIN index_records ir ON ir.index_id = i.id "
        "WHERE i.kind = 'fts' AND ir.document_id = ?",
        (document_id,),
    ):
        table = f"fts_{row['physical_name']}"
        conn.execute(f'DELETE FROM "{table}" WHERE document_id = ?', (document_id,))

    if conn.execute("SELECT 1 FROM documents WHERE content_sha256 = ? AND id != ?", (content, document_id)).fetchone():
        enqueue_task(conn, "purge_blobs", {"blobs": []})
        return []
    blobs = {content}
    keys = {r[0] for r in conn.execute("SELECT cache_key FROM parse_cache WHERE content_sha256 = ?", (content,))}
    # Pre-migration cache entries lack a content hash. Node definitions retain their route options.
    for row in conn.execute("SELECT resolved FROM nodes WHERE type = 'parse'"):
        routes = json.loads(row["resolved"])["runtime"]["routes"]
        keys.update(parse_cache_key(route["provider_fp"], route["options"], content) for route in routes)
    for key in keys:
        row = conn.execute("DELETE FROM parse_cache WHERE cache_key = ? RETURNING blob_sha256", (key,)).fetchone()
        if row is not None:
            blobs.add(row[0])
        conn.execute("DELETE FROM parse_jobs WHERE cache_key = ?", (key,))
    pending = sorted(blobs)
    enqueue_task(conn, "purge_blobs", {"blobs": pending})
    return pending


def purge_blobs(ctx: AppContext, hashes: list[str]) -> None:
    # Publishers also hold the database write transaction while storing and recording blobs.
    with ctx.db.write() as conn:
        for sha in hashes:
            if (
                conn.execute(
                    "SELECT 1 FROM documents WHERE content_sha256 = ? UNION ALL "
                    "SELECT 1 FROM parse_cache WHERE blob_sha256 = ? LIMIT 1",
                    (sha, sha),
                ).fetchone()
                is None
            ):
                ctx.blobs.delete(sha)
    status = ctx.db.one("PRAGMA wal_checkpoint(TRUNCATE)")
    if status is not None and status[0]:
        raise ExecutionError("purge_checkpoint_busy", "The WAL is in use. Retrying the purge.", retryable=True)
