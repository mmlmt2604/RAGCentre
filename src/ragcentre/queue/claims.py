"""Claiming work (section 12.2): leases, heartbeat, completion, failure and retries.

A node run is claimable when it is pending and due, or running with an expired lease, and every
upstream run for the same document has succeeded at the same document revision. One statement
inside ``BEGIN IMMEDIATE`` claims it; completion is conditional on the claim still being valid.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ragcentre.errors import ExecutionError
from ragcentre.logs import redact
from ragcentre.timeutil import after, now_iso, parse_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext


@dataclass(frozen=True)
class ClaimedRun:
    id: int
    corpus_id: str
    document_id: str
    node_fp: str
    node_type: str
    doc_revision: int
    attempt: int
    worker_id: str
    lease_expires_at: str


def _row_to_claim(row: Any) -> ClaimedRun:
    return ClaimedRun(
        id=int(row["id"]),
        corpus_id=row["corpus_id"],
        document_id=row["document_id"],
        node_fp=row["node_fp"],
        node_type=row["node_type"],
        doc_revision=int(row["doc_revision"]),
        attempt=int(row["attempt"]),
        worker_id=str(row["worker_id"]),
        lease_expires_at=str(row["lease_expires_at"]),
    )


def claim_run(ctx: AppContext, worker_id: str) -> ClaimedRun | None:
    now = now_iso()
    lease = after(ctx.settings.lease_seconds)
    with ctx.db.write() as conn:
        row = conn.execute(
            """
            UPDATE node_runs
            SET status = 'running', attempt = attempt + 1, worker_id = :worker,
                lease_expires_at = :lease, started_at = :now
            WHERE id = (
              SELECT r.id FROM node_runs r
              WHERE ((r.status = 'pending' AND r.run_after <= :now)
                  OR (r.status = 'running' AND r.lease_expires_at < :now))
                AND EXISTS (SELECT 1 FROM documents d WHERE d.id = r.document_id
                            AND d.status = 'active' AND d.revision = r.doc_revision)
                AND NOT EXISTS (
                  SELECT 1 FROM node_edges e
                  LEFT JOIN node_runs u
                    ON u.document_id = r.document_id AND u.node_fp = e.upstream_fp
                  WHERE e.node_fp = r.node_fp
                    AND (u.id IS NULL OR u.status != 'succeeded' OR u.doc_revision != r.doc_revision))
              ORDER BY r.priority DESC, r.id
              LIMIT 1)
            RETURNING id, corpus_id, document_id, node_fp, doc_revision, attempt, worker_id, lease_expires_at,
                      (SELECT n.type FROM nodes n WHERE n.fingerprint = node_runs.node_fp) AS node_type
            """,
            {"worker": worker_id, "lease": lease, "now": now},
        ).fetchone()
    return None if row is None else _row_to_claim(row)


def extend_lease(ctx: AppContext, claim: ClaimedRun) -> bool:
    new_lease = after(ctx.settings.lease_seconds)
    cur = ctx.db.execute(
        "UPDATE node_runs SET lease_expires_at = ? WHERE id = ? AND worker_id = ? AND attempt = ?",
        (new_lease, claim.id, claim.worker_id, claim.attempt),
    )
    return cur.rowcount == 1


def is_current(ctx: AppContext, claim: ClaimedRun) -> bool:
    row = ctx.db.one(
        "SELECT r.doc_revision FROM node_runs r JOIN documents d ON d.id = r.document_id "
        "WHERE r.id = ? AND r.worker_id = ? AND r.attempt = ? AND d.status = 'active' "
        "AND r.doc_revision = d.revision AND r.status IN ('running', 'succeeded')",
        (claim.id, claim.worker_id, claim.attempt),
    )
    return row is not None and int(row["doc_revision"]) == claim.doc_revision


def complete(ctx: AppContext, claim: ClaimedRun, stats: dict[str, Any]) -> bool:
    now = now_iso()
    cur = ctx.db.execute(
        "UPDATE node_runs SET status = 'succeeded', finished_at = ?, stats = ?, error_code = NULL, "
        "error_message = NULL, lease_expires_at = NULL WHERE id = ? AND worker_id = ? AND attempt = ? "
        "AND doc_revision = ?",
        (now, json.dumps(stats), claim.id, claim.worker_id, claim.attempt, claim.doc_revision),
    )
    return cur.rowcount == 1


def _backoff_seconds(attempt: int, max_attempts: int, retry_after: float | None) -> float:
    if attempt >= max_attempts:
        return 0.0
    base = min(300.0, 2.0 * 2.0**attempt)
    jittered = base * random.uniform(0.5, 1.5)
    if retry_after is not None and retry_after > jittered:
        return retry_after
    return jittered


def fail(ctx: AppContext, claim: ClaimedRun, error: ExecutionError) -> bool:
    """Record a failure; retryable errors reschedule, permanent errors fail the run."""
    permanent = not error.retryable or claim.attempt >= ctx.settings.max_attempts
    now = now_iso()
    message = redact(error.message)[:2000]
    if permanent:
        status = "failed"
        run_after = now
    else:
        status = "pending"
        run_after = after(_backoff_seconds(claim.attempt, ctx.settings.max_attempts, error.retry_after))
    cur = ctx.db.execute(
        "UPDATE node_runs SET status = ?, finished_at = ?, error_code = ?, error_message = ?, "
        "run_after = ?, lease_expires_at = NULL, worker_id = NULL WHERE id = ? AND worker_id = ? "
        "AND attempt = ? AND doc_revision = ?",
        (
            status,
            now if permanent else None,
            error.code,
            message,
            run_after,
            claim.id,
            claim.worker_id,
            claim.attempt,
            claim.doc_revision,
        ),
    )
    return cur.rowcount == 1


def _validate_lease(lease: str | None) -> bool:
    if not lease:
        return False
    try:
        return parse_iso(lease).timestamp() >= time.time() - 1
    except (ValueError, OSError):
        return False


def claim_task(ctx: AppContext, worker_id: str) -> Any | None:
    now = now_iso()
    lease = after(ctx.settings.lease_seconds)
    with ctx.db.write() as conn:
        row = conn.execute(
            "UPDATE tasks SET status = 'running', attempt = attempt + 1, worker_id = :worker, "
            "lease_expires_at = :lease WHERE id = (SELECT id FROM tasks WHERE "
            "((status = 'pending' AND run_after <= :now) OR (status = 'running' AND lease_expires_at < :now)) "
            "ORDER BY id LIMIT 1) RETURNING *",
            {"worker": worker_id, "lease": lease, "now": now},
        ).fetchone()
    return None if row is None else dict(row)


def extend_task_lease(ctx: AppContext, task_id: int, worker_id: str, attempt: int) -> bool:
    cur = ctx.db.execute(
        "UPDATE tasks SET lease_expires_at = ? WHERE id = ? AND worker_id = ? AND attempt = ? AND status = 'running'",
        (after(ctx.settings.lease_seconds), task_id, worker_id, attempt),
    )
    return cur.rowcount == 1


def complete_task(ctx: AppContext, task_id: int, worker_id: str, attempt: int) -> bool:
    cur = ctx.db.execute(
        "UPDATE tasks SET status = 'succeeded', finished_at = ?, error_message = NULL, lease_expires_at = NULL, "
        "worker_id = NULL WHERE id = ? AND worker_id = ? AND attempt = ?",
        (now_iso(), task_id, worker_id, attempt),
    )
    return cur.rowcount == 1


def fail_task(ctx: AppContext, task_id: int, worker_id: str, attempt: int, error: ExecutionError) -> bool:
    permanent = not error.retryable or attempt >= ctx.settings.max_attempts
    if permanent:
        status = "failed"
        run_after = now_iso()
    else:
        status = "pending"
        run_after = after(_backoff_seconds(attempt, ctx.settings.max_attempts, error.retry_after))
    cur = ctx.db.execute(
        "UPDATE tasks SET status = ?, error_message = ?, run_after = ?, lease_expires_at = NULL, worker_id = NULL "
        "WHERE id = ? AND worker_id = ? AND attempt = ?",
        (status, redact(error.message)[:2000], run_after, task_id, worker_id, attempt),
    )
    return cur.rowcount == 1


def new_worker_id() -> str:
    return f"{uuid.uuid4().hex[:12]}"
