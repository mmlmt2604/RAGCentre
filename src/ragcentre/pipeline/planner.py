"""The planner (section 12.1): node rows, index rows and node runs.

Every operation runs in one write transaction and uses ``INSERT OR IGNORE``, which is what makes
shared nodes deduplicate: a node already present (same fingerprint) is not inserted again, and a
document that already has a run for a node gets no second one.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from ragcentre.errors import Conflict, ErrorCode, GuardError, InvalidPipeline, NotFound
from ragcentre.hashing import collection_name, new_id
from ragcentre.locks import index_catalog_lock
from ragcentre.pipeline.graph import Resolution, resolve_pipeline
from ragcentre.timeutil import now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext

FTS_TOKENIZERS = frozenset({"unicode61", "trigram"})


def fts_table(physical_name: str) -> str:
    return f"fts_{physical_name}"


def create_fts_table(conn: sqlite3.Connection, physical_name: str, tokenizer: str) -> None:
    if tokenizer not in FTS_TOKENIZERS:  # validated by the config model; never interpolate anything else
        raise ValueError(f"unsupported tokenizer {tokenizer!r}")
    conn.execute(
        f'CREATE VIRTUAL TABLE IF NOT EXISTS "{fts_table(physical_name)}" '
        f"USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, body, tokenize='{tokenizer}')"
    )

    if sqlite3.sqlite_version_info >= (3, 42, 0):
        enable_fts_secure_delete(conn, physical_name)


def enable_fts_secure_delete(conn: sqlite3.Connection, physical_name: str) -> None:
    table = fts_table(physical_name)
    if not all(c.isascii() and (c.isalnum() or c == "_") for c in table):
        raise ValueError("Invalid FTS table name")
    enabled = conn.execute(f'SELECT v FROM "{table}_config" WHERE k = ?', ("secure-delete",)).fetchone()
    if enabled is None or enabled[0] != 1:
        conn.execute(f'INSERT INTO "{table}" ("{table}", rank) VALUES (?, ?)', ("secure-delete", 1))
        conn.execute(f'INSERT INTO "{table}" ("{table}") VALUES (?)', ("rebuild",))


def rebuild_broken_indexes(ctx: AppContext) -> int:
    """Move quarantined indexes to isolated collections and queue their ingestion again."""
    with index_catalog_lock(ctx.settings.data_dir), ctx.db.write() as conn:
        indexes = conn.execute(
            "SELECT i.*, n.resolved FROM indexes i JOIN nodes n ON n.fingerprint = i.node_fp WHERE i.broken = 1"
        ).fetchall()
        for index in indexes:
            physical = collection_name(index["corpus_id"], index["node_fp"])
            if index["kind"] == "fts":
                create_fts_table(conn, physical, json.loads(index["resolved"])["runtime"]["tokenizer"])
            conn.execute("DELETE FROM index_records WHERE index_id = ?", (index["id"],))
            conn.execute(
                "UPDATE indexes SET physical_name = ?, dim = NULL, broken = 0 WHERE id = ?", (physical, index["id"])
            )
            conn.execute(
                "UPDATE node_runs SET status='pending', attempt=attempt+1, run_after=?, worker_id=NULL, "
                "lease_expires_at=NULL, error_code=NULL, error_message=NULL WHERE corpus_id=? AND node_fp=? "
                "AND document_id IN (SELECT id FROM documents WHERE status='active')",
                (now_iso(), index["corpus_id"], index["node_fp"]),
            )
        for name in {ix["physical_name"] for ix in indexes if ix["kind"] == "fts"}:
            if not conn.execute("SELECT 1 FROM indexes WHERE kind='fts' AND physical_name=?", (name,)).fetchone():
                table = fts_table(name)
                conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        return len(indexes)


def active_node_fps(conn: sqlite3.Connection, corpus_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT pn.node_fp FROM pipeline_nodes pn JOIN pipelines p ON p.id = pn.pipeline_id "
        "WHERE p.corpus_id = ? AND p.archived = 0",
        (corpus_id,),
    ).fetchall()
    return [r[0] for r in rows]


def existing_pipeline_names(conn: sqlite3.Connection, corpus_id: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM pipelines WHERE corpus_id = ?", (corpus_id,))}


def resolve_for_corpus(ctx: AppContext, corpus_id: str, raw: Any) -> Resolution:
    with ctx.db.connection() as conn:
        names = existing_pipeline_names(conn, corpus_id)
    return resolve_pipeline(raw, ctx.providers.by_name, names)


# ---- dry run --------------------------------------------------------------------------------------


def plan(ctx: AppContext, corpus_id: str, raw: Any) -> dict[str, Any]:
    """The ``PipelinePlan`` for a submitted configuration. Saves nothing."""
    res = resolve_for_corpus(ctx, corpus_id, raw)
    if not res.valid:
        return {
            "valid": False,
            "problems": [p.to_json() for p in res.problems],
            "pipeline_fingerprint": None,
            "nodes": [],
            "total_runs_to_create": 0,
        }
    db = ctx.db
    with db.read() as conn:
        n_docs = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE corpus_id = ? AND status = 'active'", (corpus_id,)
        ).fetchone()[0]
        nodes = []
        total = 0
        for node in res.nodes:
            pipelines = conn.execute(
                "SELECT p.id, p.archived FROM pipeline_nodes pn JOIN pipelines p ON p.id = pn.pipeline_id "
                "WHERE p.corpus_id = ? AND pn.node_fp = ? ORDER BY p.created_at",
                (corpus_id, node.node_fp),
            ).fetchall()
            have = conn.execute(
                "SELECT COUNT(*) FROM node_runs r JOIN documents d ON d.id = r.document_id "
                "WHERE d.corpus_id = ? AND d.status = 'active' AND r.node_fp = ?",
                (corpus_id, node.node_fp),
            ).fetchone()[0]
            to_create = int(n_docs) - int(have)
            total += to_create
            nodes.append(
                {
                    "node_id": node.node_id,
                    "type": node.type,
                    "node_fp": node.node_fp,
                    "reused": bool(pipelines),
                    "shared_with": list(dict.fromkeys(p["id"] for p in pipelines if not p["archived"])),
                    "runs_to_create": to_create,
                }
            )
    return {
        "valid": True,
        "problems": [],
        "pipeline_fingerprint": res.fingerprint,
        "nodes": nodes,
        "total_runs_to_create": total,
    }


# ---- pipeline created ---------------------------------------------------------------------------------


def _insert_nodes(conn: sqlite3.Connection, res: Resolution, now: str) -> None:
    for node in res.nodes:
        conn.execute(
            "INSERT OR IGNORE INTO nodes (fingerprint, type, resolved, created_at) VALUES (?, ?, ?, ?)",
            (node.node_fp, node.type, json.dumps(node.to_json()), now),
        )
        for position, upstream in enumerate(node.upstream):
            conn.execute(
                "INSERT OR IGNORE INTO node_edges (node_fp, upstream_fp, position) VALUES (?, ?, ?)",
                (node.node_fp, upstream, position),
            )


def _ensure_indexes(conn: sqlite3.Connection, corpus_id: str, res: Resolution, now: str) -> None:
    for node in res.nodes:
        if node.type not in ("index_vector", "index_fts"):
            continue
        physical = collection_name(corpus_id, node.node_fp)
        kind = "vector" if node.type == "index_vector" else "fts"
        cur = conn.execute(
            "INSERT OR IGNORE INTO indexes (id, corpus_id, node_fp, kind, provider_id, physical_name, dim, metric, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                new_id(),
                corpus_id,
                node.node_fp,
                kind,
                node.runtime.get("provider_id") if kind == "vector" else None,
                physical,
                node.runtime.get("metric") if kind == "vector" else None,
                now,
            ),
        )
        if kind == "fts" and cur.rowcount:
            create_fts_table(conn, physical, node.runtime["tokenizer"])


def create_runs(conn: sqlite3.Connection, corpus_id: str, node_fps: list[str], document_id: str | None = None) -> int:
    """Pending runs for every (active document x node) that has none. Returns how many."""
    now = now_iso()
    created = 0
    for fp in node_fps:
        sql = (
            "INSERT OR IGNORE INTO node_runs (corpus_id, document_id, node_fp, doc_revision, status, priority, "
            "attempt, run_after) SELECT corpus_id, id, ?, revision, 'pending', 0, 0, ? FROM documents "
            "WHERE corpus_id = ? AND status = 'active'"
        )
        params: list[Any] = [fp, now, corpus_id]
        if document_id is not None:
            sql += " AND id = ?"
            params.append(document_id)
        created += conn.execute(sql, params).rowcount
    return created


def create_pipeline(ctx: AppContext, corpus_id: str, raw: Any) -> str:
    """Validate, save and plan a pipeline. Returns its id."""
    res = resolve_for_corpus(ctx, corpus_id, raw)
    if not res.valid:
        problems = [p.to_json() for p in res.problems]
        summary = "; ".join(f"{p['node_id'] + ': ' if p['node_id'] else ''}{p['message']}" for p in problems[:3])
        raise InvalidPipeline(f"The pipeline is not valid: {summary}", details={"problems": problems})
    assert res.config is not None and res.fingerprint is not None
    pipeline_id = new_id()
    now = now_iso()
    try:
        with index_catalog_lock(ctx.settings.data_dir), ctx.db.write() as conn:
            conn.execute(
                "INSERT INTO pipelines (id, corpus_id, name, config, resolved, fingerprint, archived, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    pipeline_id,
                    corpus_id,
                    res.config.name,
                    json.dumps(raw),
                    json.dumps(res.to_json()),
                    res.fingerprint,
                    now,
                ),
            )
            _insert_nodes(conn, res, now)
            for node in res.nodes:
                conn.execute(
                    "INSERT INTO pipeline_nodes (pipeline_id, node_id, node_fp) VALUES (?, ?, ?)",
                    (pipeline_id, node.node_id, node.node_fp),
                )
            _ensure_indexes(conn, corpus_id, res, now)
            create_runs(conn, corpus_id, [n.node_fp for n in res.nodes])
    except sqlite3.IntegrityError as exc:
        if "pipelines.corpus_id, pipelines.name" in str(exc):
            raise Conflict(f'A pipeline named "{res.config.name}" already exists in this corpus.') from None
        raise
    return pipeline_id


# ---- pipeline archived ----------------------------------------------------------------------------------


def archive_pipeline(ctx: AppContext, pipeline_id: str) -> None:
    with ctx.db.write() as conn:
        row = conn.execute("SELECT p.*, c.active_pipeline_id FROM pipelines p JOIN corpora c ON c.id = p.corpus_id "
                           "WHERE p.id = ?", (pipeline_id,)).fetchone()  # fmt: skip
        if row is None:
            raise NotFound(f"Pipeline {pipeline_id} was not found.")
        if row["active_pipeline_id"] == pipeline_id:
            raise GuardError(
                f"{row['name']} is the corpus's active pipeline. Make another pipeline active before archiving it.",
                code=ErrorCode.PIPELINE_IS_ACTIVE,
            )
        if row["archived"]:
            return
        conn.execute("UPDATE pipelines SET archived = 1 WHERE id = ?", (pipeline_id,))
        still_used = set(active_node_fps(conn, row["corpus_id"]))
        fps = [r[0] for r in conn.execute("SELECT node_fp FROM pipeline_nodes WHERE pipeline_id = ?", (pipeline_id,))]
        for fp in fps:
            if fp not in still_used:
                conn.execute(
                    "DELETE FROM node_runs WHERE corpus_id = ? AND node_fp = ? AND status = 'pending'",
                    (row["corpus_id"], fp),
                )


# ---- document events ------------------------------------------------------------------------------------


def on_document_created(conn: sqlite3.Connection, corpus_id: str, document_id: str) -> int:
    return create_runs(conn, corpus_id, active_node_fps(conn, corpus_id), document_id)


def on_document_replaced(conn: sqlite3.Connection, corpus_id: str, document_id: str) -> int:
    """Content replaced: bump the revision and reset every run of the document."""
    conn.execute("UPDATE documents SET revision = revision + 1, updated_at = ? WHERE id = ?", (now_iso(), document_id))
    revision = conn.execute("SELECT revision FROM documents WHERE id = ?", (document_id,)).fetchone()[0]
    conn.execute(
        "UPDATE node_runs SET status = 'pending', doc_revision = ?, attempt = 0, run_after = ?, lease_expires_at = NULL, "
        "worker_id = NULL, error_code = NULL, error_message = NULL WHERE document_id = ?",
        (revision, now_iso(), document_id),
    )
    # Nodes added since (by new pipelines) get their runs too.
    on_document_created(conn, corpus_id, document_id)
    return int(revision)


def reset_vector_runs(conn: sqlite3.Connection, corpus_id: str, document_id: str | None = None) -> int:
    """Metadata changed or a schema field was added: rewrite index_vector records."""
    # A running run keeps its attempt number, so its (now stale) completion no longer matches.
    sql = (
        "UPDATE node_runs SET status = 'pending', "
        "attempt = CASE WHEN status = 'running' THEN attempt ELSE 0 END, "
        "run_after = ?, lease_expires_at = NULL, worker_id = NULL, error_code = NULL, error_message = NULL "
        "WHERE corpus_id = ? AND node_fp IN (SELECT fingerprint FROM nodes WHERE type = 'index_vector') "
        "AND status IN ('succeeded', 'failed', 'running')"
    )
    params: list[Any] = [now_iso(), corpus_id]
    if document_id is not None:
        sql += " AND document_id = ?"
        params.append(document_id)
    return conn.execute(sql, params).rowcount


def on_document_deleted(conn: sqlite3.Connection, document_id: str) -> None:
    conn.execute("UPDATE documents SET status = 'deleting', updated_at = ? WHERE id = ?", (now_iso(), document_id))
    conn.execute("DELETE FROM node_runs WHERE document_id = ? AND status = 'pending'", (document_id,))
    enqueue_task(conn, "delete_document", {"document_id": document_id})


def enqueue_task(conn: sqlite3.Connection, kind: str, payload: dict[str, Any]) -> int:
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO tasks (kind, payload, status, attempt, run_after, created_at) VALUES (?, ?, 'pending', 0, ?, ?)",
        (kind, json.dumps(payload), now, now),
    )
    return int(cur.lastrowid or 0)
