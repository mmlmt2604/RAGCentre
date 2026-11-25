"""Document endpoints."""

from __future__ import annotations

import json
import mimetypes
import re
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from ragcentre.api.authz import principal_of, require_corpus, require_document
from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.api.permissions import SessionRoute
from ragcentre.context import AppContext
from ragcentre.errors import Conflict, ErrorCode, NotFound, RagError, ValidationFailed
from ragcentre.ingest import ingest_document
from ragcentre.locks import document_lock
from ragcentre.models import api
from ragcentre.models.ir import normalise_ir
from ragcentre.models.metadata import validate_metadata
from ragcentre.pipeline.planner import on_document_deleted, on_document_replaced, reset_vector_runs
from ragcentre.pipeline.purge import release_document
from ragcentre.timeutil import now_iso

router = APIRouter(route_class=SessionRoute, tags=["documents"])


def _mime_type(file: UploadFile) -> str:
    """The upload's MIME type. Clients such as ``curl -F file=@x.pdf`` send none, or the generic
    ``application/octet-stream``; guess from the filename (as the CLI does) so the file still
    matches a pipeline's ``application/pdf`` route."""
    declared = (file.content_type or "").split(";", 1)[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return file.content_type or declared
    guessed = mimetypes.guess_type(file.filename or "")[0]
    return guessed or declared or "application/octet-stream"


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
    request: Request,
    file: UploadFile = File(...),
    metadata: str | None = Form(None),
    attributes: str | None = Form(None),
    external_id: str | None = Form(None),
    ctx: AppContext = Depends(get_ctx),
) -> JSONResponse:
    require_corpus(ctx, principal_of(request), corpus_id)
    content = file.file.read(ctx.settings.max_upload_bytes + 1)
    try:
        meta = json.loads(metadata or "{}")
        attrs = json.loads(attributes or "{}")
    except json.JSONDecodeError:
        raise ValidationFailed("metadata and attributes must be valid JSON objects.") from None
    if not isinstance(meta, dict) or not isinstance(attrs, dict):
        raise ValidationFailed("metadata and attributes must be JSON objects.")
    result = ingest_document(
        ctx,
        corpus_id,
        content,
        filename=file.filename or "document",
        mime_type=_mime_type(file),
        external_id=external_id,
        metadata=meta if metadata is not None else None,
        attributes=attrs if attributes is not None else None,
    )
    return JSONResponse(
        _out(ctx, result.document_id).model_dump(mode="json"),
        status_code=201 if result.created else 200,
    )


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
    request: Request,
    q: str | None = None,
    state: DocState | None = None,
    pipeline_id: str | None = None,
    params: PageParams = Depends(page_params),
    ctx: AppContext = Depends(get_ctx),
) -> api.Page[api.Document]:
    """Active documents, newest first. ``q`` matches the filename or external id (case-insensitive);
    ``state`` filters by ingestion state, across all non-archived pipelines or only ``pipeline_id``."""
    require_corpus(ctx, principal_of(request), corpus_id)
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
def get_document(document_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.Document:
    require_document(ctx, principal_of(request), document_id)
    return _out(ctx, document_id)


@router.patch("/documents/{document_id}", response_model=api.Document)
def patch_document(
    document_id: str, body: api.DocumentPatch, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.Document:
    require_document(ctx, principal_of(request), document_id)
    with document_lock(ctx.settings.data_dir, document_id), ctx.db.write() as conn:
        d = _get(ctx, document_id)
        if d["status"] != "active":
            raise Conflict("Document is being deleted.")
        if body.metadata is not None:
            schema = json.loads(ctx.db.scalar("SELECT metadata_schema FROM corpora WHERE id = ?", (d["corpus_id"],)))
            validate_metadata(body.metadata, schema)
            conn.execute(
                "UPDATE documents SET metadata = ?, updated_at = ? WHERE id = ?",
                (json.dumps(body.metadata), now_iso(), document_id),
            )
            reset_vector_runs(conn, d["corpus_id"], document_id)
        if body.attributes is not None:
            conn.execute(
                "UPDATE documents SET attributes = ?, updated_at = ? WHERE id = ?",
                (json.dumps(body.attributes), now_iso(), document_id),
            )
        return _out(ctx, document_id)


@router.delete("/documents/{document_id}", status_code=202)
def delete_document(document_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.Document:
    require_document(ctx, principal_of(request), document_id)
    _get(ctx, document_id)
    with document_lock(ctx.settings.data_dir, document_id), ctx.db.write() as conn:
        on_document_deleted(conn, document_id)
        result = _out(ctx, document_id)
    return result


@router.put("/documents/{document_id}/content", response_model=api.Document)
def replace_content(
    document_id: str, request: Request, file: UploadFile = File(...), ctx: AppContext = Depends(get_ctx)
) -> api.Document:
    require_document(ctx, principal_of(request), document_id)
    content = file.file.read(ctx.settings.max_upload_bytes + 1)
    if len(content) > ctx.settings.max_upload_bytes:
        raise RagError("File exceeds the upload limit.", code=ErrorCode.PAYLOAD_TOO_LARGE, status=413)
    with document_lock(ctx.settings.data_dir, document_id), ctx.db.write() as conn:
        doc = _get(ctx, document_id)
        if doc["status"] != "active":
            raise Conflict("Document is being deleted.")
        sha = ctx.blobs.put(content)
        if doc["content_sha256"] != sha:
            release_document(conn, doc)
            conn.execute(
                "UPDATE documents SET content_sha256 = ?, filename = ?, mime_type = ?, size_bytes = ? WHERE id = ?",
                (
                    sha,
                    file.filename or "document",
                    _mime_type(file),
                    len(content),
                    document_id,
                ),
            )
            on_document_replaced(conn, doc["corpus_id"], document_id)
    return _out(ctx, document_id)


@router.get("/documents/{document_id}/content")
def get_content(document_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    require_document(ctx, principal_of(request), document_id)
    with document_lock(ctx.settings.data_dir, document_id):
        d = _get(ctx, document_id)
        if d["status"] != "active":
            raise NotFound(f"Document {document_id} was not found.")
        content = ctx.blobs.get(d["content_sha256"])
    mime = d["mime_type"]
    if not re.fullmatch(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+", mime):
        mime = "application/octet-stream"
    filename = re.sub(r"[\x00-\x1f\x7f]", "", d["filename"].replace(chr(92), "/").rsplit("/", 1)[-1])
    if filename in {"", ".", ".."}:
        filename = "document"
    return Response(
        content=content,
        media_type=mime,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"},
    )


def _document_pipeline(ctx: AppContext, document_id: str, pipeline_id: str) -> Any:
    doc = _get(ctx, document_id)
    if doc["status"] != "active":
        raise NotFound(f"Document {document_id} was not found.")
    pipeline = ctx.db.one("SELECT * FROM pipelines WHERE id = ? AND corpus_id = ?", (pipeline_id, doc["corpus_id"]))
    if pipeline is None:
        raise NotFound(f"Pipeline {pipeline_id} was not found in this corpus.")
    return pipeline


@router.get("/documents/{document_id}/parsed")
def get_parsed(document_id: str, pipeline_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> Any:
    require_document(ctx, principal_of(request), document_id)
    with document_lock(ctx.settings.data_dir, document_id), ctx.db.read():
        pipeline = _document_pipeline(ctx, document_id, pipeline_id)
        nodes = json.loads(pipeline["resolved"])["nodes"]
        parse_node = next((n for n in nodes if n["type"] == "parse"), None)
        if parse_node is None:
            raise NotFound("Pipeline has no parse node.")
        run = ctx.db.one(
            "SELECT r.* FROM node_runs r JOIN documents d ON d.id = r.document_id "
            "WHERE d.id = ? AND r.node_fp = ? AND r.doc_revision = d.revision",
            (document_id, parse_node["node_fp"]),
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
    document_id: str,
    pipeline_id: str,
    request: Request,
    node_id: str | None = None,
    ctx: AppContext = Depends(get_ctx),
) -> list[api.Chunk]:
    require_document(ctx, principal_of(request), document_id)
    with document_lock(ctx.settings.data_dir, document_id), ctx.db.read():
        pipeline = _document_pipeline(ctx, document_id, pipeline_id)
        resolved = json.loads(pipeline["resolved"])
        nodes = resolved["nodes"]
        text_nodes = [n for n in nodes if n["type"] in ("chunk", "enrich")]
        if node_id:
            text_node = next((n for n in text_nodes if n["node_id"] == node_id), None)
        else:
            # Without a node id: the enrich node retrieval reads, else the only chunk node.
            enrich = (resolved.get("retrieval_runtime") or {}).get("enrich")
            chunk_nodes = [n for n in text_nodes if n["type"] == "chunk"]
            if enrich is not None:
                text_node = next((n for n in text_nodes if n["node_id"] == enrich["node_id"]), None)
            else:
                text_node = chunk_nodes[0] if len(chunk_nodes) == 1 else None
        if text_node is None:
            raise ValidationFailed("Choose a chunk or enrich node, or a pipeline with exactly one chunk node.")
        chunk_fp = text_node["runtime"]["chunk_fp"] if text_node["type"] == "enrich" else text_node["node_fp"]
        rows = ctx.db.all(
            "SELECT c.*, e.enrichment AS enrichment FROM chunks c JOIN documents d ON d.id = c.document_id "
            "LEFT JOIN chunk_enrichments e ON e.enrich_node_fp = ? AND e.chunk_id = c.id "
            "AND e.doc_revision = c.doc_revision "
            "WHERE d.id = ? AND c.chunk_node_fp = ? AND c.doc_revision = d.revision ORDER BY c.ordinal",
            (text_node["node_fp"] if text_node["type"] == "enrich" else None, document_id, chunk_fp),
        )
        return [
            api.Chunk.model_validate(
                dict(
                    id=r["id"],
                    document_id=r["document_id"],
                    ordinal=r["ordinal"],
                    text=r["text"],
                    context=r["context"],
                    enrichment=r["enrichment"] or None,
                    page_start=r["page_start"],
                    page_end=r["page_end"],
                    heading_path=json.loads(r["heading_path"]),
                    block_ids=json.loads(r["block_ids"]),
                    char_count=len(r["text"]),
                )
            )
            for r in rows
        ]
