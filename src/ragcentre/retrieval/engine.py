"""The retrieval engine (section 13)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, TypeVar

from ragcentre.chunking.base import embedded_text
from ragcentre.errors import ErrorCode, ExecutionError, GuardError, NotFound, ProviderFailed, ValidationFailed
from ragcentre.models.filters import parse_filter, schema_types
from ragcentre.retrieval.expand import neighbours
from ragcentre.retrieval.fts import fts_query
from ragcentre.retrieval.fusion import SourceMatch, rrf, weighted
from ragcentre.stores.filter_sql import check_field, compile_sqlite

if TYPE_CHECKING:
    from ragcentre.context import AppContext


def _document_field(column: str, defaults: Mapping[str, Any]) -> Any:
    """Filter field expression over a ``documents`` row.

    ``document_id`` is the row id. Metadata fields fall back to the schema default, so a document
    whose metadata predates a field filters exactly like its vector records (which always carry
    every field, section 10.3).
    """
    alias = check_field(column)

    def expr(field: str) -> tuple[str, list[Any]]:
        if field == "document_id":
            return f"{alias}.id", []
        extract = f"json_extract({alias}.metadata, '$.{check_field(field)}')"
        if field in defaults:
            default = defaults[field]
            return f"COALESCE({extract}, ?)", [int(default) if isinstance(default, bool) else default]
        return extract, []

    return expr


def _pipeline(ctx: AppContext, pipeline_id: str) -> dict[str, Any]:
    row = ctx.db.one("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,))
    if row is None:
        raise NotFound(f"Pipeline {pipeline_id} was not found.")
    return dict(row)


def _index_row(ctx: AppContext, corpus_id: str, index_fp: str) -> dict[str, Any]:
    row = ctx.db.one("SELECT * FROM indexes WHERE corpus_id = ? AND node_fp = ?", (corpus_id, index_fp))
    if row is None:
        raise GuardError("An index of this pipeline has not been built yet.", code=ErrorCode.INDEX_NOT_BUILT)
    return dict(row)


def _node(ctx: AppContext, fp: str) -> dict[str, Any]:
    row = ctx.db.one("SELECT * FROM nodes WHERE fingerprint = ?", (fp,))
    assert row is not None
    return {"type": row["type"], "resolved": json.loads(row["resolved"])}


def _embedder_provider_id(ctx: AppContext, index_fp: str) -> str:
    """The embedder used for a vector source is derived from the graph (section 6)."""
    index_node = _node(ctx, index_fp)
    embed_fp = index_node["resolved"]["upstream"][0]
    embed_node = _node(ctx, embed_fp)
    return str(embed_node["resolved"]["runtime"]["provider_id"])


def _mismatch_guard(
    ctx: AppContext, pipeline: dict[str, Any], resolved: dict[str, Any], allow_partial: bool, status_fn: Any
) -> tuple[list[dict[str, Any]], bool]:
    if pipeline["archived"]:
        raise GuardError(f"{pipeline['name']} is archived and cannot be queried.", code=ErrorCode.PIPELINE_ARCHIVED)
    for source in resolved["retrieval_runtime"]["sources"]:
        index = _index_row(ctx, pipeline["corpus_id"], source["index_fp"])
        if source["kind"] == "index_fts":
            table = f"fts_{index['physical_name']}"
            if ctx.db.one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)) is None:
                raise GuardError(f"Keyword index {index['physical_name']} is missing.", code=ErrorCode.INDEX_MISMATCH)
        if source["kind"] == "index_vector":
            if index["dim"] is None:
                raise GuardError(f"Index {source['index']} has not been built yet.", code=ErrorCode.INDEX_NOT_BUILT)
            store = ctx.providers.store(index["provider_id"])
            info = store.collection_info(index["physical_name"])
            if info is None or info.dim != int(index["dim"]) or info.metric != index["metric"]:
                raise GuardError(
                    f"Index {index['physical_name']} no longer matches the pipeline's recorded configuration.",
                    code=ErrorCode.INDEX_MISMATCH,
                )
    health = status_fn(pipeline["id"])
    if not allow_partial and health.status != "ready":
        raise GuardError(
            f"{pipeline['name']} is {health.status} ({round(health.coverage * 100)}% coverage). "
            "Query it anyway by setting allow_partial: true.",
            code=ErrorCode.INDEX_INCOMPLETE,
            details={"status": health.status, "coverage": health.coverage},
        )
    return resolved["retrieval_runtime"]["sources"], health.status != "ready"


def _vector_source(
    ctx: AppContext, corpus_id: str, source: dict[str, Any], vector: list[float], f: Any
) -> tuple[list[SourceMatch], float]:
    started = time.perf_counter()
    index = _index_row(ctx, corpus_id, source["index_fp"])
    store = ctx.providers.store(index["provider_id"])
    raw = store.query(index["physical_name"], vector, source["top_k"], f)
    return (
        [SourceMatch(source["id"], m.id, m.score, rank) for rank, m in enumerate(raw, start=1)],
        (time.perf_counter() - started) * 1000,
    )


def _keyword_source(
    ctx: AppContext, corpus_id: str, source: dict[str, Any], text: str, f: Any, defaults: Mapping[str, Any]
) -> tuple[list[SourceMatch], float]:
    started = time.perf_counter()
    safe = fts_query(text)
    if safe is None:
        return [], (time.perf_counter() - started) * 1000
    index = _index_row(ctx, corpus_id, source["index_fp"])
    table = f"fts_{index['physical_name']}"
    where, params = compile_sqlite(f, _document_field("d", defaults))
    # FTS5 needs the table's own name (not an alias) on the left of MATCH and inside bm25().
    sql = (
        f'SELECT "{table}".chunk_id AS chunk_id, -bm25("{table}") AS score FROM "{table}" '
        f'JOIN documents d ON d.id = "{table}".document_id '
        f"WHERE \"{table}\" MATCH ? AND d.status = 'active' AND ({where}) "
        f'ORDER BY bm25("{table}") LIMIT ?'
    )
    rows = ctx.db.all(sql, [safe, *params, source["top_k"]])
    return (
        [
            SourceMatch(source["id"], str(r["chunk_id"]), float(r["score"]), rank)
            for rank, r in enumerate(rows, start=1)
        ],
        (time.perf_counter() - started) * 1000,
    )


T = TypeVar("T")


def _provider_call(ctx: AppContext, provider_id: str, call: Callable[[], T]) -> T:
    """The query path does not retry: a provider failure is a 502 naming the provider."""
    try:
        return call()
    except ExecutionError as exc:
        name = exc.provider or ctx.providers.get(provider_id).name
        raise ProviderFailed(f"{name} failed: {exc.message}", details={"provider": name, "cause": exc.code}) from None


def query(
    ctx: AppContext,
    corpus_id: str,
    pipeline_id: str,
    text: str,
    top_k: int | None = None,
    filter_: dict[str, Any] | None = None,
    allow_partial: bool = False,
    debug: bool = False,
) -> dict[str, Any]:
    total_started = time.perf_counter()
    pipeline = _pipeline(ctx, pipeline_id)
    if pipeline["corpus_id"] != corpus_id:
        raise ValidationFailed("That pipeline does not belong to this corpus.")
    resolved = json.loads(pipeline["resolved"])
    schema = json.loads(ctx.db.scalar("SELECT metadata_schema FROM corpora WHERE id = ?", (corpus_id,)))
    f = parse_filter(filter_, schema_types(schema))
    defaults = {name: spec["default"] for name, spec in schema.get("fields", {}).items()}
    sources, partial = _mismatch_guard(ctx, pipeline, resolved, allow_partial, ctx.pipeline_status)
    final_k = top_k if top_k is not None else int(resolved["retrieval_runtime"]["top_k"])
    timings: dict[str, float] = {}
    matches: dict[str, list[SourceMatch]] = {}

    def run_source(source: dict[str, Any]) -> tuple[str, list[SourceMatch], float]:
        if source["kind"] == "index_vector":
            embedder_id = _embedder_provider_id(ctx, source["index_fp"])
            started = time.perf_counter()
            vector = _provider_call(
                ctx, embedder_id, lambda: ctx.embeddings.embed(embedder_id, "query", [text]).vectors[0]
            )
            timings["embed"] = round((time.perf_counter() - started) * 1000, 2)
            index = _index_row(ctx, corpus_id, source["index_fp"])
            if index["dim"] is not None and len(vector) != int(index["dim"]):
                raise GuardError(
                    f"The embedder returned {len(vector)} dimensions but the index has {index['dim']}.",
                    code=ErrorCode.EMBEDDING_DIMENSION_MISMATCH,
                )
            result, elapsed = _vector_source(ctx, corpus_id, source, vector, f)
            return source["id"], result, elapsed
        result, elapsed = _keyword_source(ctx, corpus_id, source, text, f, defaults)
        return source["id"], result, elapsed

    if len(sources) == 1:
        source_id, result, elapsed = run_source(sources[0])
        matches[source_id] = result
        timings[source_id] = round(elapsed, 2)
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(sources))) as pool:
            for source_id, result, elapsed in pool.map(run_source, sources):
                matches[source_id] = result
                timings[source_id] = round(elapsed, 2)

    fusion = resolved["retrieval_runtime"].get("fusion")
    if fusion is not None:
        started = time.perf_counter()
        if fusion["method"] == "weighted":
            fused = weighted(matches, fusion["weights"])
        else:
            fused = rrf(matches, fusion["weights"], int(fusion["k"]))
        timings["fusion"] = round((time.perf_counter() - started) * 1000, 2)
        scores = dict(fused)
    else:
        only = next(iter(matches.values()))
        scores = {m.chunk_id: m.score for m in only}

    ordered = [cid for cid, _ in sorted(scores.items(), key=lambda x: (-x[1], x[0]))]
    top_ids = ordered[:final_k]

    rerank = resolved["retrieval_runtime"].get("rerank")
    if rerank is not None and ordered:
        chunks = _load_chunks(ctx, ordered[: int(rerank["top_n"])])
        texts = [embedded_text(c["text"], c["context"]) for c in chunks]
        started = time.perf_counter()
        ranked = _provider_call(
            ctx,
            rerank["provider_id"],
            lambda: ctx.providers.instance(rerank["provider_id"]).rerank(text, texts, len(texts)),
        )
        timings["rerank"] = round((time.perf_counter() - started) * 1000, 2)
        reranked = [(chunks[i]["id"], score) for i, score in ranked]
        scores = {**scores, **dict(reranked)}
        top_ids = [cid for cid, _ in reranked[:final_k]]

    results = _assemble(ctx, top_ids, matches, scores, resolved, final_k)
    timings["total"] = round((time.perf_counter() - total_started) * 1000, 2)
    return {
        "pipeline_id": pipeline_id,
        "pipeline_fingerprint": pipeline["fingerprint"],
        "partial": partial,
        "results": results,
        "timings_ms": timings,
    }


def _load_chunks(ctx: AppContext, ids: Sequence[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = ctx.db.all(f"SELECT * FROM chunks WHERE id IN ({marks})", list(ids))
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _assemble(
    ctx: AppContext,
    ids: Sequence[str],
    matches: Mapping[str, Sequence[SourceMatch]],
    scores: Mapping[str, float],
    resolved: dict[str, Any],
    top_k: int,
) -> list[dict[str, Any]]:
    chunks = _load_chunks(ctx, ids)
    by_id = {c["id"]: c for c in chunks}
    expand = int(resolved["retrieval_runtime"].get("expand", {}).get("neighbors", 0) or 0)
    out: list[dict[str, Any]] = []
    for chunk_id in ids:
        chunk = by_id.get(chunk_id)
        if chunk is None:
            continue
        doc_row = ctx.db.one("SELECT * FROM documents WHERE id = ?", (chunk["document_id"],))
        if doc_row is None or doc_row["status"] != "active":
            continue  # a document being deleted may still have records in a store
        rank = len(out) + 1
        sources: dict[str, dict[str, Any]] = {}
        for source_id, src_matches in matches.items():
            for m in src_matches:
                if m.chunk_id == chunk_id:
                    sources[source_id] = {"rank": m.rank, "score": m.score}
                    break
        context_before: list[str] = []
        context_after: list[str] = []
        if expand:
            siblings = ctx.db.all(
                "SELECT text, ordinal FROM chunks WHERE document_id = ? AND chunk_node_fp = ? ORDER BY ordinal",
                (chunk["document_id"], chunk["chunk_node_fp"]),
            )
            context_before, context_after = neighbours(siblings, int(chunk["ordinal"]), expand)
        out.append(
            {
                "chunk_id": chunk_id,
                "document_id": chunk["document_id"],
                "score": scores.get(chunk_id, 0.0),
                "rank": rank,
                "text": chunk["text"],
                "context": chunk["context"],
                "page_start": chunk["page_start"],
                "page_end": chunk["page_end"],
                "heading_path": json.loads(chunk["heading_path"]),
                "document": {
                    "filename": doc_row["filename"],
                    "external_id": doc_row["external_id"],
                    "metadata": json.loads(doc_row["metadata"]),
                    "attributes": json.loads(doc_row["attributes"]),
                },
                "context_before": context_before,
                "context_after": context_after,
                "sources": sources,
            }
        )
    return out
