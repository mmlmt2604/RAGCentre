"""Garbage collection (``POST /v1/admin/gc``, section 16).

What is garbage:

- **Indexes** (vector collections and keyword tables) whose node no non-archived pipeline of the
  index's corpus uses. They are dropped by ``drop_index`` tasks, because dropping a vector
  collection may need the store's network connection.
- **Node work**: the node runs and chunks of a ``(corpus, node fingerprint)`` pair that no
  non-archived pipeline of that corpus uses. Node definitions themselves (``nodes``) are kept,
  because archived pipelines still describe themselves with them. Pairs with a running run are
  left for a later pass.
- **Blobs** referenced by nothing: neither a document's content nor a parse-cache entry. Blobs
  written in the last ``BLOB_GRACE_SECONDS`` are kept. Deletion and replacement have a separate
  purge task for content that has lost its last document reference.

A dry run (the default) reports what would be removed and changes nothing.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ragcentre.context import AppContext
from ragcentre.pipeline.planner import enqueue_task

log = logging.getLogger("ragcentre.gc")

BLOB_GRACE_SECONDS = 600.0


@dataclass
class GcResult:
    dry_run: bool
    indexes: list[dict[str, Any]] = field(default_factory=list)
    node_count: int = 0
    blob_count: int = 0
    blob_bytes: int = 0


def _live_pairs(conn: Any) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT DISTINCT p.corpus_id, pn.node_fp FROM pipeline_nodes pn "
        "JOIN pipelines p ON p.id = pn.pipeline_id WHERE p.archived = 0"
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def _work_pairs(conn: Any) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """All (corpus, node) pairs with work, and those with a running run."""
    pairs = {(r[0], r[1]) for r in conn.execute("SELECT DISTINCT corpus_id, node_fp FROM node_runs")}
    pairs |= {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT DISTINCT d.corpus_id, c.chunk_node_fp FROM chunks c JOIN documents d ON d.id = c.document_id"
        )
    }
    running = {
        (r[0], r[1]) for r in conn.execute("SELECT DISTINCT corpus_id, node_fp FROM node_runs WHERE status = 'running'")
    }
    return pairs, running


def collect(ctx: AppContext, *, dry_run: bool = True) -> GcResult:
    result = GcResult(dry_run=dry_run)
    with ctx.db.write() as conn:
        live = _live_pairs(conn)
        indexes = [
            dict(r)
            for r in conn.execute("SELECT id, kind, physical_name, corpus_id, node_fp FROM indexes ORDER BY created_at")
            if (r["corpus_id"], r["node_fp"]) not in live
        ]
        result.indexes = [{k: ix[k] for k in ("id", "kind", "physical_name", "corpus_id")} for ix in indexes]
        pairs, running = _work_pairs(conn)
        dead = sorted(p for p in pairs if p not in live and p not in running)
        result.node_count = len(dead)
        if not dry_run:
            for corpus_id, fp in dead:
                conn.execute("DELETE FROM node_runs WHERE corpus_id = ? AND node_fp = ?", (corpus_id, fp))
                conn.execute(
                    "DELETE FROM chunks WHERE chunk_node_fp = ? AND document_id IN "
                    "(SELECT id FROM documents WHERE corpus_id = ?)",
                    (fp, corpus_id),
                )
            queued = {
                json.loads(r[0]).get("index_id")
                for r in conn.execute(
                    "SELECT payload FROM tasks WHERE kind = 'drop_index' AND status IN ('pending', 'running')"
                )
            }
            for ix in indexes:
                if ix["id"] not in queued:
                    enqueue_task(conn, "drop_index", {"index_id": ix["id"]})
        referenced = {r[0] for r in conn.execute("SELECT content_sha256 FROM documents")}
        referenced |= {r[0] for r in conn.execute("SELECT blob_sha256 FROM parse_cache")}

    cutoff = time.time() - BLOB_GRACE_SECONDS
    orphans: list[tuple[str, int]] = []
    for sha in ctx.blobs.iter_all():
        if sha in referenced:
            continue
        try:
            stat = ctx.blobs.path(sha).stat()
        except FileNotFoundError:
            continue
        if stat.st_mtime <= cutoff:
            orphans.append((sha, stat.st_size))
    result.blob_count = len(orphans)
    result.blob_bytes = sum(size for _, size in orphans)
    if not dry_run:
        with ctx.db.write() as conn:
            for sha, _ in orphans:
                if (
                    conn.execute(
                        "SELECT 1 FROM documents WHERE content_sha256 = ? UNION ALL "
                        "SELECT 1 FROM parse_cache WHERE blob_sha256 = ? LIMIT 1",
                        (sha, sha),
                    ).fetchone()
                    is None
                ):
                    ctx.blobs.delete(sha)
        log.info(
            "garbage collected",
            extra={"indexes": len(indexes), "nodes": result.node_count, "blobs": result.blob_count},
        )
    return result
