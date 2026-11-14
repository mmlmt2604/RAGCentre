"""Document endpoints."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi.responses import JSONResponse

from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.context import AppContext
from ragcentre.errors import ErrorCode, NotFound, RagError, ValidationFailed
from ragcentre.hashing import new_id
from ragcentre.locks import document_lock
from ragcentre.models import api
from ragcentre.models.ir import normalise_ir
from ragcentre.models.metadata import validate_metadata
from ragcentre.pipeline.planner import on_document_created, on_document_deleted, on_document_replaced
from ragcentre.timeutil import now_iso

router = APIRouter(tags=["documents"])


def _get(ctx: AppContext, document_id: str) -> dict[str, Any]:
    row = ctx.db.one("SELECT * FROM documents WHERE id = ?", (document_id,))
    if row is None:
        raise NotFound(f"Document {document_id} was not found.")
    return dict(row)


def _out(ctx: AppContext, document_id: str) -> api.Document:
    d = _get(ctx, document_id)
    pipelines = ctx.db.all(
        "SELECT id FROM pipelines WHERE corpus_id = ? AND archived = 0 ORDER BY created_at", (d["corpus_id"],)
    )
    ingestion = []
    for pipeline in pipelines:
        pipeline_nodes = json.loads(ctx.db.scalar("SELECT resolved FROM pipelines WHERE id = ?", (pipeline["id"],)))[
            "nodes"
        ]
        states = []
        for pn in pipeline_nodes:
            run = ctx.db.one(
                "SELECT * FROM node_runs WHERE document_id = ? AND node_fp = ?",
                (document_id, pn["node_fp"]),
            )
            if run is None:
                states.append(
                    api.DocumentIngestionNode(
                        node_id=pn["node_id"], node_fp=pn["node_fp"], run_id=0, status="pending", error_code=None
                    )
                )
            else:
                effective = run["status"]
                if effective == "pending":
                    from ragcentre.pipeline.status import blocking_run

                    if blocking_run(ctx, run) is not None:
                        effective = "blocked"
                states.append(
                    api.DocumentIngestionNode(
                        node_id=pn["node_id"],
                        node_fp=pn["node_fp"],
                        run_id=run["id"],
                        status=effective,
                        error_code=run["error_code"],
                    )
                )
        state: Any
        if any(n.status == "failed" for n in states):
            state = "failed"
        elif any(n.status == "running" for n in states):
            state = "running"
        elif all(n.status == "succeeded" for n in states):
            state = "succeeded"
        else:
            state = "pending"
        ingestion.append(api.DocumentIngestion(pipeline_id=pipeline["id"], state=state, nodes=states))
    return api.Document.model_validate(
        dict(
            id=d["id"],
            corpus_id=d["corpus_id"],
            external_id=d["external_id"],
            filename=d["filename"],
            mime_type=d["mime_type"],
            size_bytes=d["size_bytes"],
            content_sha256=d["content_sha256"],
            metadata=json.loads(d["metadata"]),
            attributes=json.loads(d["attributes"]),
            revision=d["revision"],
            status=d["status"],
            ingestion=ingestion,
            created_at=d["created_at"],
            updated_at=d["updated_at"],
        )
    )


@router.post(
    "/corpora/{corpus_id}/documents",
    response_model=None,
    status_code=201,
    responses={
        200: {"model": api.Document, "description": "external_id matched an existing document"},
        201: {"model": api.Document, "description": "A new document"},
    },
)
def upload(
    corpus_id: str,
    file: UploadFile = File(...),
    metadata: str = Form("{}"),
    attributes: str = Form("{}"),
    external_id: str | None = Form(None),
    ctx: AppContext = Depends(get_ctx),
) -> JSONResponse:
    content = file.file.read(ctx.settings.max_upload_bytes + 1)
    if len(content) > ctx.settings.max_upload_bytes:
        raise RagError("File is larger than the upload limit.", code=ErrorCode.PAYLOAD_TOO_LARGE, status=413)
    try:
        meta = json.loads(metadata or "{}")
        attrs = json.loads(attributes or "{}")
    except json.JSONDecodeError:
        raise ValidationFailed("metadata and attributes must be valid JSON objects.") from None
    if not isinstance(meta, dict) or not isinstance(attrs, dict):
        raise ValidationFailed("metadata and attributes must be JSON objects.")
    corpus = ctx.db.one("SELECT * FROM corpora WHERE id = ?", (corpus_id,))
    if corpus is None:
        raise NotFound(f"Corpus {corpus_id} was not found.")
    schema = json.loads(corpus["metadata_schema"])
    validate_metadata(meta, schema)
    sha = ctx.blobs.put(content)
    doc_id = new_id()
    now = now_iso()
    existing = (
        ctx.db.one(
            "SELECT * FROM documents WHERE corpus_id = ? AND external_id = ? AND external_id IS NOT NULL",
            (corpus_id, external_id),
        )
        if external_id
        else None
    )
    if existing is not None and existing["content_sha256"] != sha:
        with document_lock(ctx.settings.data_dir, existing["id"]), ctx.db.write() as conn:
            conn.execute(
                "UPDATE documents SET content_sha256 = ?, filename = ?, mime_type = ?, size_bytes = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    sha,
                    file.filename,
                    file.content_type or "application/octet-stream",
                    len(content),
                    now,
                    existing["id"],
                ),
            )
            on_document_replaced(conn, corpus_id, existing["id"])
        return JSONResponse(_out(ctx, existing["id"]).model_dump(mode="json"), status_code=200)
    if existing is not None and existing["content_sha256"] == sha:
        return JSONResponse(_out(ctx, existing["id"]).model_dump(mode="json"), status_code=200)
    with ctx.db.write() as conn:
        conn.execute(
            "INSERT INTO documents (id, corpus_id, external_id, filename, mime_type, size_bytes, content_sha256, "
            "metadata, attributes, revision, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)",
            (
                doc_id,
                corpus_id,
                external_id,
                file.filename,
                file.content_type or "application/octet-stream",
                len(content),
                sha,
                json.dumps(meta),
                json.dumps(attrs),
                now,
                now,
            ),
        )
        on_document_created(conn, corpus_id, doc_id)
    return JSONResponse(_out(ctx, doc_id).model_dump(mode="json"), status_code=201)


DocState = Literal["pending", "running", "succeeded", "failed"]

# A document's ingestion state across the live nodes of the corpus's non-archived pipelines (or of
# one pipeline): failed if any run failed; else running if any is running; else succeeded if every
# node has succeeded; else pending (blocked runs count as pending). The same rule as
# DocumentIngestion.state, applied to the union of nodes.
_STATE_SQL: dict[str, str] = {
    "failed": "f > 0",
    "running": "f = 0 AND ru > 0",
    "succeeded": "f = 0 AND ru = 0 AND s = (SELECT COUNT(*) FROM live)",
    "pending": "f = 0 AND ru = 0 AND s < (SELECT COUNT(*) FROM live)",
}


@router.get("/corpora/{corpus_id}/documents", response_model=api.Page[api.Document])
def list_documents(
    corpus_id: str,
    q: str | None = None,
    state: DocState | None = None,
    pipeline_id: str | None = None,
    params: PageParams = Depends(page_params),
    ctx: AppContext = Depends(get_ctx),
) -> api.Page[api.Document]:
    """Active documents, newest first. ``q`` matches the filename or external id (case-insensitive);
    ``state`` filters by ingestion state, across all non-archived pipelines or only ``pipeline_id``."""
    if ctx.db.one("SELECT id FROM corpora WHERE id = ?", (corpus_id,)) is None:
        raise NotFound(f"Corpus {corpus_id} was not found.")
    live_sql = (
        "SELECT DISTINCT pn.node_fp AS fp FROM pipeline_nodes pn JOIN pipelines p ON p.id = pn.pipeline_id "
        "WHERE p.corpus_id = ? AND p.archived = 0"
    )
    args: list[Any] = [corpus_id]
    if pipeline_id is not None:
        if ctx.db.one("SELECT id FROM pipelines WHERE id = ? AND corpus_id = ?", (pipeline_id, corpus_id)) is None:
            raise NotFound(f"Pipeline {pipeline_id} was not found in this corpus.")
        live_sql += " AND p.id = ?"
        args.append(pipeline_id)
    where = "d.corpus_id = ? AND d.status = 'active'"
    args.append(corpus_id)
    if q:
        where += (
            " AND (instr(lower(d.filename), lower(?)) > 0 OR instr(lower(COALESCE(d.external_id, '')), lower(?)) > 0)"
        )
        args += [q, q]
    sql = (
        f"WITH live(fp) AS ({live_sql}), agg AS ("
        "SELECT d.id, d.created_at, "
        "COALESCE(SUM(r.status = 'failed'), 0) AS f, COALESCE(SUM(r.status = 'running'), 0) AS ru, "
        "COALESCE(SUM(r.status = 'succeeded'), 0) AS s "
        "FROM documents d LEFT JOIN node_runs r ON r.document_id = d.id AND r.node_fp IN (SELECT fp FROM live) "
        f"WHERE {where} GROUP BY d.id) "
        f"SELECT id FROM agg {'WHERE ' + _STATE_SQL[state] if state else ''} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    rows = ctx.db.all(sql, (*args, params.limit + 1, params.offset))
    items = [_out(ctx, r["id"]) for r in rows[: params.limit]]
    return api.Page[api.Document](
        items=items, next_cursor=str(params.offset + params.limit) if len(rows) > params.limit else None
    )


@router.get("/documents/{document_id}", response_model=api.Document)
def get_document(document_id: str, ctx: AppContext = Depends(get_ctx)) -> api.Document:
    return _out(ctx, document_id)


@router.patch("/documents/{document_id}", response_model=api.Document)
def patch_document(document_id: str, body: api.DocumentPatch, ctx: AppContext = Depends(get_ctx)) -> api.Document:
    d = _get(ctx, document_id)
    if body.metadata is not None:
        schema = json.loads(ctx.db.scalar("SELECT metadata_schema FROM corpora WHERE id = ?", (d["corpus_id"],)))
        validate_metadata(body.metadata, schema)
        with document_lock(ctx.settings.data_dir, document_id), ctx.db.write() as conn:
            conn.execute(
                "UPDATE documents SET metadata = ?, updated_at = ? WHERE id = ?",
                (json.dumps(body.metadata), now_iso(), document_id),
            )
            from ragcentre.pipeline.planner import reset_vector_runs

            reset_vector_runs(conn, d["corpus_id"], document_id)
    if body.attributes is not None:
        ctx.db.execute(
            "UPDATE documents SET attributes = ?, updated_at = ? WHERE id = ?",
            (json.dumps(body.attributes), now_iso(), document_id),
        )
    return _out(ctx, document_id)


@router.delete("/documents/{document_id}", status_code=202)
def delete_document(document_id: str, ctx: AppContext = Depends(get_ctx)) -> api.Document:
    _get(ctx, document_id)
    with document_lock(ctx.settings.data_dir, document_id), ctx.db.write() as conn:
        on_document_deleted(conn, document_id)
        result = _out(ctx, document_id)
    return result


@router.put("/documents/{document_id}/content", response_model=api.Document)
def replace_content(document_id: str, file: UploadFile = File(...), ctx: AppContext = Depends(get_ctx)) -> api.Document:
    content = file.file.read(ctx.settings.max_upload_bytes + 1)
    if len(content) > ctx.settings.max_upload_bytes:
        raise RagError("File exceeds the upload limit.", code=ErrorCode.PAYLOAD_TOO_LARGE, status=413)
    sha = ctx.blobs.put(content)
    with document_lock(ctx.settings.data_dir, document_id), ctx.db.write() as conn:
        doc = _get(ctx, document_id)
        if doc["status"] != "active":
            raise ValidationFailed("Document is being deleted.")
        if doc["content_sha256"] != sha:
            conn.execute(
                "UPDATE documents SET content_sha256 = ?, filename = ?, mime_type = ?, size_bytes = ? WHERE id = ?",
                (
                    sha,
                    file.filename or "document",
                    file.content_type or "application/octet-stream",
                    len(content),
                    document_id,
                ),
            )
            on_document_replaced(conn, doc["corpus_id"], document_id)
    return _out(ctx, document_id)


@router.get("/documents/{document_id}/content")
def get_content(document_id: str, ctx: AppContext = Depends(get_ctx)) -> Response:
    d = _get(ctx, document_id)
    content = ctx.blobs.get(d["content_sha256"])
    from fastapi.responses import Response as FastAPIResponse

    return FastAPIResponse(content=content, media_type=d["mime_type"])


@router.get("/documents/{document_id}/parsed")
def get_parsed(document_id: str, pipeline_id: str, ctx: AppContext = Depends(get_ctx)) -> Any:
    _get(ctx, document_id)
    pipeline = ctx.db.one("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,))
    if pipeline is None:
        raise NotFound(f"Pipeline {pipeline_id} was not found.")
    nodes = json.loads(pipeline["resolved"])["nodes"]
    parse_node = next((n for n in nodes if n["type"] == "parse"), None)
    if parse_node is None:
        raise NotFound("Pipeline has no parse node.")
    run = ctx.db.one(
        "SELECT * FROM node_runs WHERE document_id = ? AND node_fp = ?", (document_id, parse_node["node_fp"])
    )
    if run is None or run["status"] != "succeeded":
        raise NotFound("The parse run has not succeeded for this document.")
    from ragcentre.pipeline.executors import _parse_blob

    data = _parse_blob(ctx, parse_node["node_fp"], document_id)
    ir = normalise_ir(data)
    out = ir.model_dump(mode="json")
    for page in out.get("pages", []):
        page.pop("image", None)
    return out


@router.get("/documents/{document_id}/chunks", response_model=list[api.Chunk])
def get_chunks(
    document_id: str, pipeline_id: str, node_id: str | None = None, ctx: AppContext = Depends(get_ctx)
) -> list[api.Chunk]:
    pipeline = ctx.db.one("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,))
    if pipeline is None:
        raise NotFound(f"Pipeline {pipeline_id} was not found.")
    nodes = json.loads(pipeline["resolved"])["nodes"]
    chunk_nodes = [n for n in nodes if n["type"] == "chunk"]
    if node_id:
        chunk_node = next((n for n in chunk_nodes if n["node_id"] == node_id), None)
    else:
        chunk_node = chunk_nodes[0] if len(chunk_nodes) == 1 else None
    if chunk_node is None:
        raise ValidationFailed("Choose a chunk node, or a pipeline with exactly one chunk node.")
    rows = ctx.db.all(
        "SELECT * FROM chunks WHERE document_id = ? AND chunk_node_fp = ? ORDER BY ordinal",
        (document_id, chunk_node["node_fp"]),
    )
    return [
        api.Chunk.model_validate(
            dict(
                id=r["id"],
                document_id=r["document_id"],
                ordinal=r["ordinal"],
                text=r["text"],
                context=r["context"],
                page_start=r["page_start"],
                page_end=r["page_end"],
                heading_path=json.loads(r["heading_path"]),
                block_ids=json.loads(r["block_ids"]),
                char_count=len(r["text"]),
            )
        )
        for r in rows
    ]
