"""Evaluation runs (section 14.5): creation, the resumable ``eval_run`` task, and read models."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ragcentre.errors import ErrorCode, ExecutionError, GuardError, NotFound, RagError, ValidationFailed
from ragcentre.eval.compare import paired_bootstrap
from ragcentre.eval.datasets import items as dataset_items
from ragcentre.eval.matching import matched_targets
from ragcentre.eval.metrics import DEFAULT_K_VALUES, item_metrics, metric_names, percentile, zero_metrics
from ragcentre.hashing import new_id
from ragcentre.logs import redact
from ragcentre.pipeline.planner import enqueue_task
from ragcentre.timeutil import now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext


# ---- creation ---------------------------------------------------------------------------------


def create_run(
    ctx: AppContext,
    dataset_id: str,
    pipeline_ids: list[str],
    k_values: list[int] | None = None,
    allow_partial: bool = False,
) -> str:
    dataset = ctx.db.one("SELECT * FROM eval_datasets WHERE id = ?", (dataset_id,))
    if dataset is None:
        raise NotFound(f"Dataset {dataset_id} was not found.")
    if not pipeline_ids:
        raise ValidationFailed("Choose at least one pipeline.")
    if len(set(pipeline_ids)) != len(pipeline_ids):
        raise ValidationFailed("Each pipeline may appear only once in a run.")
    ks = sorted(set(k_values or DEFAULT_K_VALUES))
    if not all(1 <= k <= 1000 for k in ks):
        raise ValidationFailed("k values must be between 1 and 1000.")
    for pid in pipeline_ids:
        p = ctx.db.one("SELECT * FROM pipelines WHERE id = ?", (pid,))
        if p is None:
            raise NotFound(f"Pipeline {pid} was not found.")
        if p["corpus_id"] != dataset["corpus_id"]:
            raise ValidationFailed(f"Pipeline {p['name']} belongs to another corpus than the dataset.")
        if p["archived"]:
            raise GuardError(f"{p['name']} is archived.", code=ErrorCode.PIPELINE_ARCHIVED)
        if not allow_partial:
            health = ctx.pipeline_status(pid)
            if health.status != "ready":
                raise GuardError(
                    f"{p['name']} is {health.status} ({round(health.coverage * 100)}% coverage). Comparing a "
                    "partly built index is misleading; wait for it, or set allow_partial.",
                    code=ErrorCode.INDEX_INCOMPLETE,
                    details={"pipeline_id": pid, "status": health.status, "coverage": health.coverage},
                )
    run_id = new_id()
    config = {"pipeline_ids": pipeline_ids, "k_values": ks, "allow_partial": allow_partial}
    with ctx.db.write() as conn:
        conn.execute(
            "INSERT INTO eval_runs (id, dataset_id, config, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (run_id, dataset_id, json.dumps(config), now_iso()),
        )
        enqueue_task(conn, "eval_run", {"run_id": run_id})
    return run_id


# ---- the task ---------------------------------------------------------------------------------


def _label(ctx: AppContext, document_id: str, cache: dict[str, str]) -> str:
    if document_id not in cache:
        row = ctx.db.one("SELECT external_id, filename FROM documents WHERE id = ?", (document_id,))
        cache[document_id] = (row["external_id"] or row["filename"]) if row else document_id
    return cache[document_id]


def run_eval(ctx: AppContext, run_id: str) -> None:
    """Query every (item, pipeline) pair that has no result yet, then write the summaries.

    Resumable: rows already written are skipped, so an interrupted run continues where it stopped.
    """
    from ragcentre.pipeline.purge import scrub_retrieved
    from ragcentre.retrieval.engine import query

    run = ctx.db.one("SELECT * FROM eval_runs WHERE id = ?", (run_id,))
    if run is None or run["status"] == "finished":
        return
    config = json.loads(run["config"])
    corpus_id = ctx.db.scalar("SELECT corpus_id FROM eval_datasets WHERE id = ?", (run["dataset_id"],))
    ks: list[int] = config["k_values"]
    ctx.db.execute("UPDATE eval_runs SET status = 'running' WHERE id = ? AND status = 'pending'", (run_id,))
    labels: dict[str, str] = {}
    for item in dataset_items(ctx, run["dataset_id"]):
        for pid in config["pipeline_ids"]:
            done = ctx.db.one(
                "SELECT 1 FROM eval_results WHERE run_id = ? AND pipeline_id = ? AND item_id = ?",
                (run_id, pid, item["id"]),
            )
            if done is not None:
                continue
            started = time.perf_counter()
            retrieved: list[dict[str, Any]] = []
            error = None
            try:
                response = query(
                    ctx,
                    corpus_id,
                    pid,
                    item["question"],
                    max(ks),
                    item["filter"],
                    bool(config["allow_partial"]),
                    include_document_revision=True,
                )
                for r in response["results"]:
                    retrieved.append(
                        {
                            "rank": r["rank"],
                            "chunk_id": r["chunk_id"],
                            "document_id": r["document_id"],
                            "document_revision": r["document_revision"],
                            "document_label": _label(ctx, r["document_id"], labels),
                            "text": r["text"],
                            "page_start": r["page_start"],
                            "page_end": r["page_end"],
                            "matched_targets": matched_targets(r, item["targets"]),
                        }
                    )
                metrics = item_metrics([r["matched_targets"] for r in retrieved], len(item["targets"]), ks)
            except RagError as exc:
                error = redact(f"{exc.error_code.value}: {exc.message}")
                metrics = zero_metrics(ks)
            except ExecutionError as exc:
                error = redact(f"{exc.code}: {exc.message}")
                metrics = zero_metrics(ks)
            latency = round((time.perf_counter() - started) * 1000, 2)
            with ctx.db.write() as conn:
                for i, entry in enumerate(retrieved):
                    current = conn.execute(
                        "SELECT 1 FROM documents WHERE id = ? AND revision = ? AND status = 'active'",
                        (entry["document_id"], entry.pop("document_revision")),
                    ).fetchone()
                    if current is None:
                        retrieved[i] = scrub_retrieved(entry)
                conn.execute(
                    "INSERT OR IGNORE INTO eval_results (run_id, pipeline_id, item_id, retrieved, metrics, latency_ms, "
                    "error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, pid, item["id"], json.dumps(retrieved), json.dumps(metrics), latency, error),
                )
    _write_summaries(ctx, run_id, config, corpus_id)


def _chunk_stats(ctx: AppContext, pipeline_id: str, corpus_id: str) -> tuple[int, float]:
    resolved = json.loads(ctx.db.scalar("SELECT resolved FROM pipelines WHERE id = ?", (pipeline_id,)))
    by_fp = {n["node_fp"]: n for n in resolved["nodes"]}
    node = by_fp.get(resolved["retrieval_runtime"]["sources"][0]["index_fp"])
    while node is not None and node["type"] != "chunk":
        node = by_fp.get(node["upstream"][0]) if node["upstream"] else None
    if node is None:
        return 0, 0.0
    row = ctx.db.one(
        "SELECT COUNT(*) AS n, AVG(LENGTH(c.text)) AS mean FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE c.chunk_node_fp = ? AND d.corpus_id = ? AND d.status = 'active'",
        (node["node_fp"], corpus_id),
    )
    return (int(row["n"]), round(float(row["mean"] or 0.0), 2)) if row else (0, 0.0)


def _write_summaries(ctx: AppContext, run_id: str, config: dict[str, Any], corpus_id: str) -> None:
    n_items = ctx.db.scalar(
        "SELECT COUNT(*) FROM eval_items WHERE dataset_id = (SELECT dataset_id FROM eval_runs WHERE id = ?)",
        (run_id,),
    )
    names = metric_names(config["k_values"])
    summaries = []
    for pid in config["pipeline_ids"]:
        rows = ctx.db.all("SELECT * FROM eval_results WHERE run_id = ? AND pipeline_id = ?", (run_id, pid))
        if len(rows) < n_items:
            return  # not complete; the task will be resumed
        per = [json.loads(r["metrics"]) for r in rows]
        latencies = [float(r["latency_ms"]) for r in rows if r["latency_ms"] is not None and r["error"] is None]
        chunk_count, mean_chars = _chunk_stats(ctx, pid, corpus_id)
        summary = {
            "metrics": {m: (sum(p[m] for p in per) / len(per) if per else 0.0) for m in names},
            "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
            "error_count": sum(1 for r in rows if r["error"] is not None),
            "chunk_count": chunk_count,
            "mean_chunk_chars": mean_chars,
        }
        fp = ctx.db.scalar("SELECT fingerprint FROM pipelines WHERE id = ?", (pid,))
        summaries.append((run_id, pid, fp, json.dumps(summary)))
    with ctx.db.write() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO eval_summaries (run_id, pipeline_id, pipeline_fp, metrics) VALUES (?, ?, ?, ?)",
            summaries,
        )
        conn.execute("UPDATE eval_runs SET status = 'finished', finished_at = ? WHERE id = ?", (now_iso(), run_id))


# ---- read models -------------------------------------------------------------------------------


def _run_row(ctx: AppContext, run_id: str) -> Any:
    row = ctx.db.one(
        "SELECT r.*, d.corpus_id, d.name AS dataset_name FROM eval_runs r JOIN eval_datasets d ON d.id = r.dataset_id "
        "WHERE r.id = ?",
        (run_id,),
    )
    if row is None:
        raise NotFound(f"Evaluation run {run_id} was not found.")
    return row


def run_out(ctx: AppContext, run_id: str) -> dict[str, Any]:
    row = _run_row(ctx, run_id)
    config = json.loads(row["config"])
    n_items = ctx.db.scalar("SELECT COUNT(*) FROM eval_items WHERE dataset_id = ?", (row["dataset_id"],))
    done = ctx.db.scalar("SELECT COUNT(*) FROM eval_results WHERE run_id = ?", (run_id,))
    return {
        "id": row["id"],
        "corpus_id": row["corpus_id"],
        "dataset_id": row["dataset_id"],
        "dataset_name": row["dataset_name"],
        "pipeline_ids": config["pipeline_ids"],
        "k_values": config["k_values"],
        "status": row["status"],
        "progress": {"done": int(done), "total": int(n_items) * len(config["pipeline_ids"])},
        "item_count": int(n_items),
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


def _per_item(ctx: AppContext, run_id: str, pipeline_id: str) -> dict[str, dict[str, Any]]:
    rows = ctx.db.all("SELECT * FROM eval_results WHERE run_id = ? AND pipeline_id = ?", (run_id, pipeline_id))
    return {
        r["item_id"]: {
            "pipeline_id": pipeline_id,
            "metrics": json.loads(r["metrics"]),
            "latency_ms": r["latency_ms"],
            "error": r["error"],
            "retrieved": json.loads(r["retrieved"] or "[]"),
        }
        for r in rows
    }


def run_detail(ctx: AppContext, run_id: str) -> dict[str, Any]:
    out = run_out(ctx, run_id)
    out["summaries"] = []
    out["comparisons"] = []
    if out["status"] != "finished":
        return out
    pids: list[str] = out["pipeline_ids"]
    for pid in pids:
        s = ctx.db.one("SELECT * FROM eval_summaries WHERE run_id = ? AND pipeline_id = ?", (run_id, pid))
        p = ctx.db.one("SELECT name, archived FROM pipelines WHERE id = ?", (pid,))
        if s is None:
            continue
        out["summaries"].append(
            {
                "pipeline_id": pid,
                "pipeline_name": p["name"] if p else "(deleted)",
                "pipeline_fingerprint": s["pipeline_fp"],
                "pipeline_archived": bool(p["archived"]) if p else True,
                **json.loads(s["metrics"]),
            }
        )
    item_ids = [it["id"] for it in dataset_items(ctx, out["dataset_id"])]
    per = {pid: _per_item(ctx, run_id, pid) for pid in pids}
    names = metric_names(out["k_values"])
    for i, a in enumerate(pids):
        for b in pids[i + 1 :]:
            for metric in names:
                va = [per[a][iid]["metrics"][metric] for iid in item_ids]
                vb = [per[b][iid]["metrics"][metric] for iid in item_ids]
                c = paired_bootstrap(va, vb)
                out["comparisons"].append(
                    {
                        "a": a,
                        "b": b,
                        "metric": metric,
                        "mean_a": c.mean_a,
                        "mean_b": c.mean_b,
                        "diff": c.diff,
                        "ci_low": c.ci_low,
                        "ci_high": c.ci_high,
                        "significant": c.significant,
                    }
                )
    return out


OUTCOMES = ("improved", "regressed", "unchanged")


def item_comparisons(
    ctx: AppContext, run_id: str, a: str, b: str, metric: str, only: str | None
) -> list[dict[str, Any]]:
    run = run_out(ctx, run_id)
    for pid in (a, b):
        if pid not in run["pipeline_ids"]:
            raise ValidationFailed(f"Pipeline {pid} is not part of this run.")
    if metric not in metric_names(run["k_values"]):
        raise ValidationFailed(f"Unknown metric {metric!r} for this run.")
    if only is not None and only not in (*OUTCOMES, "differs"):
        raise ValidationFailed("only must be differs, improved, regressed or unchanged.")
    ra, rb = _per_item(ctx, run_id, a), _per_item(ctx, run_id, b)
    out = []
    for item in dataset_items(ctx, run["dataset_id"]):
        if item["id"] not in ra or item["id"] not in rb:
            continue
        va, vb = ra[item["id"]]["metrics"][metric], rb[item["id"]]["metrics"][metric]
        outcome = "improved" if vb > va else "regressed" if vb < va else "unchanged"
        if only == "differs" and outcome == "unchanged":
            continue
        if only in OUTCOMES and outcome != only:
            continue
        out.append({"item": item, "a": ra[item["id"]], "b": rb[item["id"]], "outcome": outcome})
    return out


def mark_failed(ctx: AppContext, run_id: str) -> None:
    ctx.db.execute(
        "UPDATE eval_runs SET status = 'failed', finished_at = ? WHERE id = ? AND status != 'finished'",
        (now_iso(), run_id),
    )
