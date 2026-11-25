"""Query endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ragcentre.api.authz import principal_of, require_corpus
from ragcentre.api.deps import get_ctx
from ragcentre.context import AppContext
from ragcentre.errors import NotFound
from ragcentre.models import api
from ragcentre.retrieval.engine import query as run_query

router = APIRouter(tags=["query"])


@router.post("/corpora/{corpus_id}/query", response_model=api.QueryResponse)
def query_corpus(
    corpus_id: str, body: api.QueryRequest, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.QueryResponse:
    require_corpus(ctx, principal_of(request), corpus_id)
    pipeline_id = body.pipeline_id
    if pipeline_id is None:
        pipeline_id = ctx.db.scalar("SELECT active_pipeline_id FROM corpora WHERE id = ?", (corpus_id,))
        if pipeline_id is None:
            raise NotFound("This corpus has no active pipeline; pass pipeline_id explicitly.")
    result = run_query(
        ctx,
        corpus_id,
        pipeline_id,
        body.query,
        body.top_k,
        body.filter,
        body.allow_partial,
        body.debug,
        transform=body.transform,
    )
    return api.QueryResponse.model_validate(result)
