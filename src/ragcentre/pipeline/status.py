"""Derived run states, pipeline coverage and API projections (section 12.6)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ragcentre.context import AppContext
from ragcentre.errors import NotFound
from ragcentre.models import api

STATES = ("pending", "running", "succeeded", "failed", "blocked")


def get_pipeline(ctx: AppContext, pipeline_id: str) -> sqlite3.Row:
    row = ctx.db.one("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,))
    if row is None:
        raise NotFound(f"Pipeline {pipeline_id} was not found.")
    return row


def blocking_run(ctx: AppContext, run: sqlite3.Row) -> int | None:
    row = ctx.db.one(
        "WITH RECURSIVE upstream(fp) AS (SELECT upstream_fp FROM node_edges WHERE node_fp = ? "
        "UNION SELECT e.upstream_fp FROM node_edges e JOIN upstream u ON e.node_fp = u.fp) "
        "SELECT r.id FROM node_runs r JOIN upstream u ON u.fp = r.node_fp "
        "WHERE r.document_id = ? AND r.status = 'failed' ORDER BY r.id LIMIT 1",
        (run["node_fp"], run["document_id"]),
    )
    return int(row[0]) if row else None


def run_state(ctx: AppContext, run: sqlite3.Row) -> str:
    if run["status"] == "pending" and blocking_run(ctx, run) is not None:
        return "blocked"
    return str(run["status"])


def run_out(ctx: AppContext, run: sqlite3.Row) -> api.NodeRun:
    state = run_state(ctx, run)
    data = {
        k: run[k]
        for k in (
            "id",
            "corpus_id",
            "document_id",
            "node_fp",
            "attempt",
            "error_code",
            "error_message",
            "run_after",
            "started_at",
            "finished_at",
        )
    }
    data.update(
        document_filename=ctx.db.scalar("SELECT filename FROM documents WHERE id = ?", (run["document_id"],))
        or "(deleted)",
        node_type=ctx.db.scalar("SELECT type FROM nodes WHERE fingerprint = ?", (run["node_fp"],)),
        status=state,
        max_attempts=ctx.settings.max_attempts,
        blocked_by_run_id=blocking_run(ctx, run) if state == "blocked" else None,
        stats=json.loads(run["stats"]) if run["stats"] else None,
        pipelines=[
            dict(r)
            for r in ctx.db.all(
                "SELECT p.id AS pipeline_id, p.name AS pipeline_name, pn.node_id FROM pipeline_nodes pn "
                "JOIN pipelines p ON p.id = pn.pipeline_id WHERE pn.node_fp = ? AND p.corpus_id = ? AND p.archived = 0",
                (run["node_fp"], run["corpus_id"]),
            )
        ],
    )
    return api.NodeRun.model_validate(data)


def status(ctx: AppContext, pipeline_id: str) -> api.PipelineStatus:
    pipeline = get_pipeline(ctx, pipeline_id)
    nodes = json.loads(pipeline["resolved"])["nodes"]
    docs = ctx.db.all("SELECT id FROM documents WHERE corpus_id = ? AND status = 'active'", (pipeline["corpus_id"],))
    runs = {
        (r["document_id"], r["node_fp"]): r
        for r in ctx.db.all(
            "SELECT r.* FROM node_runs r JOIN documents d ON d.id = r.document_id "
            "WHERE r.corpus_id = ? AND d.status = 'active'",
            (pipeline["corpus_id"],),
        )
    }
    counts = {n["node_fp"]: dict.fromkeys(STATES, 0) for n in nodes}
    covered = 0
    failures: dict[int, sqlite3.Row] = {}
    for doc in docs:
        complete = True
        for n in nodes:
            run = runs.get((doc["id"], n["node_fp"]))
            effective = run_state(ctx, run) if run else "pending"
            counts[n["node_fp"]][effective] += 1
            if n["type"].startswith("index_") and effective != "succeeded":
                complete = False
            if run and effective == "failed":
                failures[run["id"]] = run
        covered += complete
    progress = any(c["pending"] or c["running"] for c in counts.values())
    failed = any(c["failed"] or c["blocked"] for c in counts.values())
    overall = "building" if progress else "degraded" if failed else "ready"
    return api.PipelineStatus.model_validate(
        dict(
            pipeline_id=pipeline_id,
            status=overall,
            coverage=covered / len(docs) if docs else 1.0,
            document_count=len(docs),
            nodes=[
                dict(node_id=n["node_id"], type=n["type"], node_fp=n["node_fp"], counts=counts[n["node_fp"]])
                for n in nodes
            ],
            recent_failures=[
                run_out(ctx, r)
                for r in sorted(failures.values(), key=lambda r: r["finished_at"] or "", reverse=True)[:20]
            ],
        )
    )


def shared_with(ctx: AppContext, corpus_id: str, fp: str, excluding: str) -> list[str]:
    return [
        r[0]
        for r in ctx.db.all(
            "SELECT DISTINCT p.id FROM pipelines p JOIN pipeline_nodes pn ON pn.pipeline_id = p.id "
            "WHERE p.corpus_id = ? AND p.archived = 0 AND pn.node_fp = ? AND p.id != ? ORDER BY p.created_at",
            (corpus_id, fp, excluding),
        )
    ]


def pipeline_out(ctx: AppContext, pipeline_id: str) -> api.Pipeline:
    row = get_pipeline(ctx, pipeline_id)
    health = status(ctx, pipeline_id)
    data: dict[str, Any] = {k: row[k] for k in ("id", "name", "fingerprint", "archived", "created_at", "corpus_id")}
    data.update(
        status=health.status,
        coverage=health.coverage,
        config=json.loads(row["config"]),
        is_active=ctx.db.scalar("SELECT active_pipeline_id FROM corpora WHERE id = ?", (row["corpus_id"],))
        == pipeline_id,
        nodes=[
            dict(
                node_id=n["node_id"],
                type=n["type"],
                node_fp=n["node_fp"],
                shared_with=shared_with(ctx, row["corpus_id"], n["node_fp"], pipeline_id),
            )
            for n in json.loads(row["resolved"])["nodes"]
        ],
    )
    return api.Pipeline.model_validate(data)
