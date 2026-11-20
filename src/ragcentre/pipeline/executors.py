"""The five node executors (section 12.4). Every executor is idempotent and returns a stats dict.

Reads happen outside write transactions; writes happen in short transactions guarded by the
conditional completion check in the queue. HTTP calls are never made inside a write transaction.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ragcentre.chunking import run_strategy
from ragcentre.errors import ErrorCode, ExecutionError
from ragcentre.hashing import chunk_id, parse_cache_key
from ragcentre.locks import document_lock
from ragcentre.models.ir import IRError, normalise_ir
from ragcentre.providers.base import invalid_response
from ragcentre.queue.claims import is_current
from ragcentre.stores.base import Record, delete_batched, upsert_batched

if TYPE_CHECKING:
    from ragcentre.context import AppContext

ProviderBuilder = Callable[[str], Any]


def _node(ctx: AppContext, fp: str) -> dict[str, Any]:
    row = ctx.db.one("SELECT * FROM nodes WHERE fingerprint = ?", (fp,))
    if row is None:
        raise ExecutionError("node_missing", f"node {fp} does not exist")
    return {"type": row["type"], "resolved": json.loads(row["resolved"])}


def _doc(ctx: AppContext, document_id: str) -> dict[str, Any]:
    row = ctx.db.one("SELECT * FROM documents WHERE id = ?", (document_id,))
    if row is None:
        raise ExecutionError("not_found", f"document {document_id} was not found")
    return dict(row)


def _upstream_run(ctx: AppContext, document_id: str, upstream_fp: str) -> dict[str, Any]:
    row = ctx.db.one("SELECT * FROM node_runs WHERE document_id = ? AND node_fp = ?", (document_id, upstream_fp))
    if row is None or row["status"] != "succeeded":
        raise ExecutionError("upstream_missing", f"upstream node {upstream_fp} has not succeeded")
    return dict(row)


def _parse_route(node: dict[str, Any], mime: str) -> dict[str, Any] | None:
    import fnmatch

    for item in node["resolved"]["runtime"]["routes"]:
        route: dict[str, Any] = item
        if fnmatch.fnmatch(mime, route["mime"]):
            return route
    return None


class _ParseJob:
    """Remembers the async job of one parse in ``parse_jobs`` (keyed by the parse cache key), so a
    worker that restarts, or a retry after a timeout, resumes polling instead of submitting again."""

    def __init__(self, ctx: AppContext, cache_key: str, provider_id: str) -> None:
        self.ctx, self.cache_key, self.provider_id = ctx, cache_key, provider_id

    def get(self) -> str | None:
        row = self.ctx.db.one(
            "SELECT job_id FROM parse_jobs WHERE cache_key = ? AND provider_id = ?", (self.cache_key, self.provider_id)
        )
        return None if row is None else str(row["job_id"])

    def save(self, job_id: str) -> None:
        with self.ctx.db.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO parse_jobs (cache_key, provider_id, job_id, created_at) "
                "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (self.cache_key, self.provider_id, job_id),
            )

    def clear(self) -> None:
        with self.ctx.db.write() as conn:
            conn.execute("DELETE FROM parse_jobs WHERE cache_key = ?", (self.cache_key,))


def execute_parse(ctx: AppContext, claim: Any) -> dict[str, Any]:
    started = time.perf_counter()
    doc = _doc(ctx, claim.document_id)
    node = _node(ctx, claim.node_fp)
    route = _parse_route(node, str(doc["mime_type"]))
    if route is None:
        raise ExecutionError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE.value,
            f"No parse route matches {doc['mime_type']}.",
            provider=doc.get("filename"),
        )
    provider_id = route["provider_id"]
    content = ctx.blobs.get(doc["content_sha256"])
    cache_key = parse_cache_key(route["provider_fp"], route["options"], doc["content_sha256"])
    row = ctx.db.one("SELECT blob_sha256 FROM parse_cache WHERE cache_key = ?", (cache_key,))
    if row is not None:
        stats: dict[str, Any] = {"duration_ms": round((time.perf_counter() - started) * 1000, 2), "cache_hit": True}
        return stats
    parser = ctx.providers.instance(provider_id)
    provider = ctx.providers.get(provider_id)
    if provider.type == "http":
        info = parser.info()
        pinned = str(provider.config.get("service_version", ""))
        if info.version != pinned and provider.config.get("on_version_drift", "fail") == "fail":
            raise ExecutionError(
                ErrorCode.PROVIDER_VERSION_DRIFT.value,
                f"{provider.name} now reports version {info.version}; it was pinned at {pinned} when created.",
                provider=provider.name,
            )
    if provider.type == "http":
        raw = parser.parse(
            content,
            str(doc["filename"]),
            str(doc["mime_type"]),
            route["options"],
            job=_ParseJob(ctx, cache_key, provider_id),
        )
    else:
        raw = parser.parse(content, str(doc["filename"]), str(doc["mime_type"]), route["options"])
    try:
        ir = normalise_ir(raw)
    except IRError as exc:
        raise invalid_response(provider.name, str(exc)) from None
    # Nothing reads page images after parsing (the UI strips them, chunk providers never see them),
    # and they can be megabytes per page, so the cache blob does not keep them.
    blob = ctx.blobs.put_json_gz(ir.without_page_images())
    with ctx.db.write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO parse_cache (cache_key, blob_sha256, created_at) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (cache_key, blob),
        )
    return {
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "cache_hit": False,
        "blocks": len(ir.blocks),
    }


def _parse_blob(ctx: AppContext, parse_fp: str, document_id: str) -> Any:
    """Load the parsed IR through the parse cache, re-deriving the key from the parse node."""
    doc = _doc(ctx, document_id)
    parse_node = _node(ctx, parse_fp)
    route = _parse_route(parse_node, str(doc["mime_type"]))
    if route is None:
        raise ExecutionError(ErrorCode.UNSUPPORTED_MEDIA_TYPE.value, f"No parse route matches {doc['mime_type']}.")
    cache_key = parse_cache_key(route["provider_fp"], route["options"], doc["content_sha256"])
    row = ctx.db.one("SELECT blob_sha256 FROM parse_cache WHERE cache_key = ?", (cache_key,))
    if row is None:
        raise ExecutionError("parse_missing", "The document has not been parsed yet.")
    return ctx.blobs.get_json_gz(row["blob_sha256"])


def _load_chunks(ctx: AppContext, chunk_fp: str, document_id: str) -> list[dict[str, Any]]:
    rows = ctx.db.all(
        "SELECT * FROM chunks WHERE chunk_node_fp = ? AND document_id = ? ORDER BY ordinal", (chunk_fp, document_id)
    )
    return [dict(r) for r in rows]


def execute_chunk(ctx: AppContext, claim: Any) -> dict[str, Any]:
    started = time.perf_counter()
    node = _node(ctx, claim.node_fp)
    runtime = node["resolved"]["runtime"]
    parse_fp = node["resolved"]["upstream"][0]
    raw_ir = _parse_blob(ctx, parse_fp, claim.document_id)
    ir = normalise_ir(raw_ir)
    provider = None
    if runtime.get("provider_id"):
        provider = ctx.providers.instance(runtime["provider_id"])
        row = ctx.providers.get(runtime["provider_id"])
        if (
            row.type == "http"
            and provider.info().version != row.config["service_version"]
            and row.config.get("on_version_drift", "fail") == "fail"
        ):
            raise ExecutionError("provider_version_drift", f"{row.name} reports a different service version")
    chunks = run_strategy(runtime["strategy"], ir, runtime["options"], provider)
    with ctx.db.write() as conn:
        if not is_current(ctx, claim):
            return {"duration_ms": 0.0}
        conn.execute(
            "DELETE FROM chunks WHERE chunk_node_fp = ? AND document_id = ?", (claim.node_fp, claim.document_id)
        )
        for chunk in chunks:
            cid = chunk_id(claim.node_fp, claim.document_id, chunk.ordinal)
            conn.execute(
                "INSERT INTO chunks (id, chunk_node_fp, document_id, ordinal, text, context, page_start, page_end, "
                "heading_path, block_ids, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    claim.node_fp,
                    claim.document_id,
                    chunk.ordinal,
                    chunk.text,
                    chunk.context,
                    chunk.page_start,
                    chunk.page_end,
                    json.dumps(chunk.heading_path),
                    json.dumps(chunk.block_ids),
                    json.dumps(chunk.metadata),
                ),
            )
    stats: dict[str, Any] = {"duration_ms": round((time.perf_counter() - started) * 1000, 2), "n_chunks": len(chunks)}
    if not chunks:
        stats["warning"] = "no_chunks"
    return stats


def _schema_defaults(ctx: AppContext, corpus_id: str) -> dict[str, Any]:
    schema = json.loads(ctx.db.scalar("SELECT metadata_schema FROM corpora WHERE id = ?", (corpus_id,)))
    return {name: spec["default"] for name, spec in schema.get("fields", {}).items()}


def _vector_records(ctx: AppContext, chunks: Sequence[dict[str, Any]], vectors: Sequence[list[float]],
                    corpus_id: str, document_id: str) -> list[Record]:  # fmt: skip
    defaults = _schema_defaults(ctx, corpus_id)
    doc_meta = json.loads(ctx.db.scalar("SELECT metadata FROM documents WHERE id = ?", (document_id,)))
    merged = {**defaults, **doc_meta}
    return [
        Record(id=chunk["id"], vector=vectors[i], metadata={"document_id": document_id, **merged})
        for i, chunk in enumerate(chunks)
    ]


def execute_embed(ctx: AppContext, claim: Any) -> dict[str, Any]:
    started = time.perf_counter()
    node = _node(ctx, claim.node_fp)
    chunk_fp = node["resolved"]["upstream"][0]
    provider_id = node["resolved"]["runtime"]["provider_id"]
    chunks = _load_chunks(ctx, chunk_fp, claim.document_id)
    res = ctx.embeddings.embed(provider_id, "document", [_embedded(c) for c in chunks])
    return {
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "n_texts": res.n_texts,
        "n_cache_hits": res.n_cache_hits,
    }


def execute_index_vector(ctx: AppContext, claim: Any) -> dict[str, Any]:
    started = time.perf_counter()
    node = _node(ctx, claim.node_fp)
    embed_fp = node["resolved"]["upstream"][0]
    store_provider_id = node["resolved"]["runtime"]["provider_id"]
    metric = node["resolved"]["runtime"]["metric"]
    embed_node = _node(ctx, embed_fp)
    embedder_provider_id = embed_node["resolved"]["runtime"]["provider_id"]
    chunk_fp = embed_node["resolved"]["upstream"][0]
    chunks = _load_chunks(ctx, chunk_fp, claim.document_id)
    res = ctx.embeddings.embed(embedder_provider_id, "document", [_embedded(c) for c in chunks])
    vectors = res.vectors
    doc_meta = json.loads(ctx.db.scalar("SELECT metadata FROM documents WHERE id = ?", (claim.document_id,)))
    schema = json.loads(ctx.db.scalar("SELECT metadata_schema FROM corpora WHERE id = ?", (claim.corpus_id,)))
    defaults = {name: spec["default"] for name, spec in schema.get("fields", {}).items()}
    field_types = {name: spec["type"] for name, spec in schema.get("fields", {}).items()}
    merged = {**defaults, **doc_meta}
    records = [
        Record(id=chunk["id"], vector=vectors[i], metadata={"document_id": claim.document_id, **merged})
        for i, chunk in enumerate(chunks)
    ]
    index_row = ctx.db.one(
        "SELECT * FROM indexes WHERE corpus_id = ? AND node_fp = ?", (claim.corpus_id, claim.node_fp)
    )
    if index_row is None:
        raise ExecutionError("index_missing", f"index for node {claim.node_fp} does not exist")
    store = ctx.providers.store(store_provider_id)
    from ragcentre.stores.base import CollectionSpec, DimensionLimitExceeded

    physical = index_row["physical_name"]
    with document_lock(ctx.settings.data_dir, claim.document_id):
        if not is_current(ctx, claim):
            return {"duration_ms": 0.0}
        # Ensure the physical collection before recording its dimension; retries also repair
        # a crash between these two operations. ensure_collection arbitrates concurrent builders.
        dim = ctx.db.scalar("SELECT dim FROM indexes WHERE id = ?", (index_row["id"],))
        if vectors:
            wanted = len(vectors[0])
            if dim is not None and wanted != dim:
                raise ExecutionError("embedding_dimension_mismatch", f"Expected {dim} dimensions, received {wanted}")
            try:
                store.ensure_collection(CollectionSpec(physical, wanted, metric, {"document_id": "str", **field_types}))
            except DimensionLimitExceeded as exc:
                raise ExecutionError("store_dimension_limit", str(exc)) from exc
            ctx.db.execute("UPDATE indexes SET dim = ? WHERE id = ? AND dim IS NULL", (wanted, index_row["id"]))
        previous = {
            r[0]
            for r in ctx.db.all(
                "SELECT record_id FROM index_records WHERE index_id = ? AND document_id = ?",
                (index_row["id"], claim.document_id),
            )
        }
        new_ids = {r.id for r in records}
        if records:
            upsert_batched(store, physical, records)
        if previous - new_ids:
            delete_batched(store, physical, list(previous - new_ids))
        with ctx.db.write() as conn:
            conn.execute(
                "DELETE FROM index_records WHERE index_id = ? AND document_id = ?", (index_row["id"], claim.document_id)
            )
            conn.executemany(
                "INSERT INTO index_records (index_id, document_id, record_id) VALUES (?, ?, ?)",
                [(index_row["id"], claim.document_id, rid) for rid in new_ids],
            )
    return {"duration_ms": round((time.perf_counter() - started) * 1000, 2), "n_records": len(records)}


def execute_index_fts(ctx: AppContext, claim: Any) -> dict[str, Any]:
    started = time.perf_counter()
    node = _node(ctx, claim.node_fp)
    chunk_fp = node["resolved"]["upstream"][0]
    chunks = _load_chunks(ctx, chunk_fp, claim.document_id)
    index_row = ctx.db.one(
        "SELECT * FROM indexes WHERE corpus_id = ? AND node_fp = ?", (claim.corpus_id, claim.node_fp)
    )
    if index_row is None:
        raise ExecutionError("index_missing", f"index for node {claim.node_fp} does not exist")
    table = f"fts_{index_row['physical_name']}"
    with ctx.db.write() as conn:
        if not is_current(ctx, claim):
            return {"duration_ms": 0.0}
        conn.execute(f'DELETE FROM "{table}" WHERE document_id = ?', (claim.document_id,))
        conn.executemany(
            f'INSERT INTO "{table}" (chunk_id, document_id, body) VALUES (?, ?, ?)',
            [(c["id"], claim.document_id, c["embedded"] if "embedded" in c else _embedded(c)) for c in chunks],
        )
        conn.execute(
            "DELETE FROM index_records WHERE index_id = ? AND document_id = ?", (index_row["id"], claim.document_id)
        )
        conn.executemany(
            "INSERT OR REPLACE INTO index_records (index_id, document_id, record_id) VALUES (?, ?, ?)",
            [(index_row["id"], claim.document_id, c["id"]) for c in chunks],
        )
    return {"duration_ms": round((time.perf_counter() - started) * 1000, 2), "n_records": len(chunks)}


def _embedded(chunk: Mapping[str, Any]) -> str:
    from ragcentre.chunking.base import embedded_text

    return embedded_text(str(chunk["text"]), chunk["context"])


def execute(ctx: AppContext, claim: Any) -> dict[str, Any]:
    if not is_current(ctx, claim):
        return {"duration_ms": 0.0}
    node_type = claim.node_type
    if node_type == "parse":
        return execute_parse(ctx, claim)
    if node_type == "chunk":
        return execute_chunk(ctx, claim)
    if node_type == "embed":
        return execute_embed(ctx, claim)
    if node_type == "index_vector":
        return execute_index_vector(ctx, claim)
    if node_type == "index_fts":
        return execute_index_fts(ctx, claim)
    raise ExecutionError("unknown_node", f"unknown node type {node_type}")
