"""The sync engine: turn a connector's remote items into documents via ``ingest_document``.

``enqueue_sync`` is the single compare-and-set that schedules a ``sync_source`` task (used by both
the scheduler tick and the "sync now" route), so two processes can never enqueue the same source
twice. ``run_sync_source`` is the task body.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ragcentre.connectors.base import FetchedFile, RemoteItem
from ragcentre.context import AppContext
from ragcentre.errors import ExecutionError
from ragcentre.ingest import ingest_document
from ragcentre.pipeline.planner import enqueue_task, on_document_deleted
from ragcentre.timeutil import after, now_iso

log = logging.getLogger("ragcentre.connectors")


def enqueue_sync(ctx: AppContext, source_id: str) -> bool:
    """Claim the source for a sync and enqueue exactly one ``sync_source`` task.

    Returns True when this call enqueued the task; False when a sync is already in flight.
    """
    now = now_iso()
    with ctx.db.write() as conn:
        cur = conn.execute(
            "UPDATE sync_sources SET status = 'syncing', last_error = NULL, updated_at = ? "
            "WHERE id = ? AND status != 'syncing'",
            (now, source_id),
        )
        if cur.rowcount == 1:
            enqueue_task(conn, "sync_source", {"source_id": source_id})
            return True
    return False


def _set_item(
    conn: Any,
    source_id: str,
    item: RemoteItem,
    *,
    document_id: str | None,
    skipped_reason: str | None,
) -> None:
    conn.execute(
        "INSERT INTO source_items (source_id, remote_id, remote_version, document_id, remote_path, "
        "remote_mime, remote_size, remote_modified, skipped_reason, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (source_id, remote_id) DO UPDATE SET "
        "remote_version = excluded.remote_version, document_id = excluded.document_id, "
        "remote_path = excluded.remote_path, remote_mime = excluded.remote_mime, "
        "remote_size = excluded.remote_size, remote_modified = excluded.remote_modified, "
        "skipped_reason = excluded.skipped_reason, last_seen_at = excluded.last_seen_at",
        (
            source_id,
            item.remote_id,
            item.version or "",
            document_id,
            item.path,
            item.mime_type,
            item.size,
            item.modified_at,
            skipped_reason,
            now_iso(),
        ),
    )


def _mark_seen(ctx: AppContext, source_id: str, item: RemoteItem) -> None:
    with ctx.db.write() as conn:
        conn.execute(
            "UPDATE source_items SET last_seen_at = ?, remote_size = ?, remote_modified = ?, "
            "remote_mime = ?, remote_path = ? WHERE source_id = ? AND remote_id = ?",
            (now_iso(), item.size, item.modified_at, item.mime_type, item.path, source_id, item.remote_id),
        )


def _delete_item(ctx: AppContext, source_id: str, remote_id: str, document_id: str | None) -> None:
    if document_id is not None:
        with ctx.db.write() as conn:
            on_document_deleted(conn, document_id)
            conn.execute("DELETE FROM source_items WHERE source_id = ? AND remote_id = ?", (source_id, remote_id))
    else:
        ctx.db.execute("DELETE FROM source_items WHERE source_id = ? AND remote_id = ?", (source_id, remote_id))


def run_sync_source(ctx: AppContext, source_id: str) -> None:
    """One sync pass for a source. Errors propagate to the task machinery, which retries retryable
    ones and marks the source ``error`` (via ``on_task_failed``) on a permanent failure."""
    source = ctx.db.one("SELECT * FROM sync_sources WHERE id = ?", (source_id,))
    if source is None:
        return  # deleted mid-flight
    schedule = int(source["schedule_seconds"])
    corpus_id = source["corpus_id"]
    ownership = ctx.db.one(
        "SELECT p.team_id, p.kind, c.team_id AS corpus_team FROM providers p JOIN corpora c ON c.id=? WHERE p.id=?",
        (corpus_id, source["provider_id"]),
    )
    if ownership is None or ownership["kind"] != "connector" or ownership["team_id"] != ownership["corpus_team"]:
        raise ExecutionError("invalid_source", "The source connector does not belong to its corpus's team.", retryable=False)
    scope = json.loads(source["scope"])
    tmpl_meta = json.loads(source["metadata_template"])
    tmpl_attrs = json.loads(source["attributes_template"])
    cursor = source["cursor"]
    seen: set[str] = set()
    full = False
    resume_cursor: str | None = None

    with ctx.providers.lease(source["provider_id"]) as conn:
        while True:
            page = conn.scan(scope, cursor)
            full = full or page.full
            if page.resume_cursor is not None:
                resume_cursor = page.resume_cursor
            for item in page.items:
                if item.deleted:
                    prev = ctx.db.one(
                        "SELECT document_id FROM source_items WHERE source_id = ? AND remote_id = ?",
                        (source_id, item.remote_id),
                    )
                    _delete_item(ctx, source_id, item.remote_id, prev["document_id"] if prev is not None else None)
                    continue
                seen.add(item.remote_id)
                prev = ctx.db.one(
                    "SELECT remote_version, document_id FROM source_items WHERE source_id = ? AND remote_id = ?",
                    (source_id, item.remote_id),
                )
                if prev is not None and prev["remote_version"] == (item.version or ""):
                    _mark_seen(ctx, source_id, item)
                    continue
                fetched: FetchedFile = conn.fetch(scope, item)
                if len(fetched.content) > ctx.settings.max_upload_bytes:
                    with ctx.db.write() as c:
                        _set_item(
                            c,
                            source_id,
                            item,
                            document_id=prev["document_id"] if prev is not None else None,
                            skipped_reason="too_large",
                        )
                    continue
                result = ingest_document(
                    ctx,
                    corpus_id,
                    fetched.content,
                    filename=fetched.filename,
                    mime_type=fetched.mime_type,
                    external_id=f"{source_id}:{item.remote_id}",
                    metadata={**tmpl_meta, **fetched.metadata},
                    attributes={**tmpl_attrs, **fetched.attributes},
                )
                with ctx.db.write() as c:
                    _set_item(c, source_id, item, document_id=result.document_id, skipped_reason=None)
            # Persist the page-continuation cursor so a crash mid-scan resumes from here
            # (re-scanning a page is safe because external_id upserts are idempotent).
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
            with ctx.db.write() as c:
                c.execute("UPDATE sync_sources SET cursor = ? WHERE id = ?", (cursor, source_id))

        if full:
            # A complete listing is the truth: delete items that vanished from the source.
            for row in ctx.db.all(
                "SELECT remote_id, document_id FROM source_items WHERE source_id = ?", (source_id,)
            ):
                if row["remote_id"] in seen:
                    continue
                _delete_item(ctx, source_id, row["remote_id"], row["document_id"])

    next_run = after(schedule) if schedule else None
    now = now_iso()
    with ctx.db.write() as c:
        c.execute(
            "UPDATE sync_sources SET status = 'idle', last_run_at = ?, last_error = NULL, next_run_at = ?, "
            "cursor = ?, updated_at = ? WHERE id = ? AND status = 'syncing'",
            (now, next_run, None if full else resume_cursor, now, source_id),
        )
