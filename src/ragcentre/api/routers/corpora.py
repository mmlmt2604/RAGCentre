"""Corpus endpoints."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends

from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.context import AppContext
from ragcentre.errors import Conflict, ErrorCode, GuardError, ImmutableField, NotFound, ValidationFailed
from ragcentre.hashing import new_id
from ragcentre.models import api
from ragcentre.models.metadata import validate_schema
from ragcentre.pipeline.planner import reset_vector_runs
from ragcentre.pipeline.status import pipeline_out
from ragcentre.timeutil import now_iso

router = APIRouter(tags=["corpora"])


def _get(ctx: AppContext, corpus_id: str) -> dict[str, object]:
    row = ctx.db.one("SELECT * FROM corpora WHERE id = ?", (corpus_id,))
    if row is None:
        raise NotFound(f"Corpus {corpus_id} was not found.")
    return dict(row)


def _out(ctx: AppContext, corpus_id: str) -> api.Corpus:
    c = _get(ctx, corpus_id)
    pipelines = [
        pipeline_out(ctx, r["id"])
        for r in ctx.db.all(
            "SELECT id FROM pipelines WHERE corpus_id = ? AND archived = 0 ORDER BY created_at", (corpus_id,)
        )
    ]
    return api.Corpus.model_validate(
        dict(
            id=c["id"],
            name=c["name"],
            metadata_schema=json.loads(str(c["metadata_schema"])),
            active_pipeline_id=str(c["active_pipeline_id"]) if c["active_pipeline_id"] is not None else None,
            document_count=ctx.db.scalar(
                "SELECT COUNT(*) FROM documents WHERE corpus_id = ? AND status = 'active'", (corpus_id,)
            ),
            pipelines=pipelines,
            created_at=c["created_at"],
        )
    )


@router.post("/corpora", response_model=api.Corpus, status_code=201)
def create_corpus(body: api.CorpusCreate, ctx: AppContext = Depends(get_ctx)) -> api.Corpus:
    corpus_id = new_id()
    now = now_iso()
    if not body.name.strip():
        raise ValidationFailed("Give the corpus a name.")
    validate_schema(body.metadata_schema.model_dump())
    try:
        ctx.db.execute(
            "INSERT INTO corpora (id, name, metadata_schema, created_at) VALUES (?, ?, ?, ?)",
            (corpus_id, body.name, json.dumps(body.metadata_schema.model_dump()), now),
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"A corpus named {body.name!r} already exists.") from None
    return _out(ctx, corpus_id)


@router.get("/corpora", response_model=api.Page[api.Corpus])
def list_corpora(params: PageParams = Depends(page_params), ctx: AppContext = Depends(get_ctx)) -> api.Page[api.Corpus]:
    rows = ctx.db.all("SELECT id FROM corpora ORDER BY created_at LIMIT ? OFFSET ?", (params.limit + 1, params.offset))
    items = [_out(ctx, r["id"]) for r in rows[: params.limit]]
    return api.Page[api.Corpus](
        items=items, next_cursor=str(params.offset + params.limit) if len(rows) > params.limit else None
    )


@router.patch("/corpora/{corpus_id}/metadata-schema", response_model=api.Corpus)
def update_schema(corpus_id: str, body: api.MetadataSchema, ctx: AppContext = Depends(get_ctx)) -> api.Corpus:
    validate_schema(body.model_dump())
    with ctx.db.write() as conn:
        previous = json.loads(str(_get(ctx, corpus_id)["metadata_schema"]))
        incoming = body.model_dump()
        for name, spec in previous.get("fields", {}).items():
            if name not in incoming["fields"] or incoming["fields"][name] != spec:
                raise ImmutableField(f"Existing field {name} and its default cannot be changed or removed.")
        if incoming != previous:
            conn.execute("UPDATE corpora SET metadata_schema = ? WHERE id = ?", (json.dumps(incoming), corpus_id))
            reset_vector_runs(conn, corpus_id)
    return _out(ctx, corpus_id)


@router.patch("/corpora/{corpus_id}", response_model=api.Corpus)
def rename_corpus(corpus_id: str, body: api.CorpusPatch, ctx: AppContext = Depends(get_ctx)) -> api.Corpus:
    """Rename a corpus. Ids, documents and pipelines are unaffected."""
    name = body.name.strip()
    if not name:
        raise ValidationFailed("Give the corpus a name.")
    _get(ctx, corpus_id)
    try:
        ctx.db.execute("UPDATE corpora SET name = ? WHERE id = ?", (name, corpus_id))
    except sqlite3.IntegrityError:
        raise Conflict(f"A corpus named {name!r} already exists.") from None
    return _out(ctx, corpus_id)


@router.get("/corpora/{corpus_id}", response_model=api.Corpus)
def get_corpus(corpus_id: str, ctx: AppContext = Depends(get_ctx)) -> api.Corpus:
    return _out(ctx, corpus_id)


@router.put("/corpora/{corpus_id}/active-pipeline", response_model=api.Corpus)
def set_active(corpus_id: str, body: api.ActivePipelineSet, ctx: AppContext = Depends(get_ctx)) -> api.Corpus:
    _get(ctx, corpus_id)
    pipeline = ctx.db.one("SELECT * FROM pipelines WHERE id = ? AND corpus_id = ?", (body.pipeline_id, corpus_id))
    if pipeline is None:
        raise NotFound(f"Pipeline {body.pipeline_id} was not found.")
    if pipeline["archived"]:
        raise GuardError(f"{pipeline['name']} is archived and cannot be made active.", code=ErrorCode.PIPELINE_ARCHIVED)
    health = ctx.pipeline_status(pipeline["id"])
    if health.status != "ready" and not body.force:
        raise GuardError(
            f"{pipeline['name']} is {health.status} ({round(health.coverage * 100)}% coverage). Confirm to make it active anyway.",
            code=ErrorCode.INDEX_INCOMPLETE,
            details={"status": health.status, "coverage": health.coverage},
        )
    ctx.db.execute("UPDATE corpora SET active_pipeline_id = ? WHERE id = ?", (body.pipeline_id, corpus_id))
    return _out(ctx, corpus_id)
