"""Pipeline endpoints: create, dry-run, list, read, status, diff, retry, archive."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ragcentre.api.authz import principal_of, require_corpus, require_pipeline
from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.api.permissions import SessionRoute
from ragcentre.context import AppContext
from ragcentre.errors import NotFound
from ragcentre.models import api
from ragcentre.pipeline.planner import archive_pipeline, create_pipeline, plan
from ragcentre.pipeline.status import get_pipeline, pipeline_out, status

router = APIRouter(route_class=SessionRoute, tags=["pipelines"])


def _diff(ctx: AppContext, a: dict[str, Any], b: dict[str, Any]) -> api.PipelineDiff:
    ca, cb = json.loads(a["config"]), json.loads(b["config"])
    ma, mb = {n["id"]: n for n in ca["nodes"]}, {n["id"]: n for n in cb["nodes"]}
    fa, fb = (
        {n["node_id"]: n for n in json.loads(a["resolved"])["nodes"]},
        {n["node_id"]: n for n in json.loads(b["resolved"])["nodes"]},
    )
    nodes = []
    for node_id in dict.fromkeys([*ma, *mb]):
        in_a, in_b = node_id in ma, node_id in mb
        same = in_a and in_b and fa[node_id]["node_fp"] == fb[node_id]["node_fp"]
        changes: list[dict[str, Any]] = []
        if in_a and in_b and not same:
            for k in sorted(set(ma[node_id]) | set(mb[node_id])):
                va, vb = ma[node_id].get(k), mb[node_id].get(k)
                if va != vb:
                    changes.append({"path": k, "a": va, "b": vb})
        nodes.append(
            dict(
                node_id=node_id,
                type=ma[node_id]["type"] if in_a else mb[node_id]["type"],
                in_a=in_a,
                in_b=in_b,
                same=same,
                changes=changes,
            )
        )
    ra, rb = ca["retrieval"], cb["retrieval"]
    # Keys from both sides, so a setting only b has (say, a reranker) is reported too.
    retrieval_changes = [
        {"path": k, "a": ra.get(k), "b": rb.get(k)} for k in dict.fromkeys([*ra, *rb]) if ra.get(k) != rb.get(k)
    ]
    return api.PipelineDiff.model_validate(
        dict(a=a["id"], b=b["id"], nodes=nodes, retrieval_same=ra == rb, retrieval_changes=retrieval_changes)
    )


@router.post(
    "/corpora/{corpus_id}/pipelines",
    response_model=None,
    status_code=201,
    responses={
        200: {"model": api.PipelinePlan, "description": "With ?dry_run=true: the plan (always 200)"},
        201: {"model": api.Pipeline, "description": "The created pipeline"},
    },
)
def create(
    corpus_id: str,
    body: dict[str, Any],
    request: Request,
    dry_run: bool = False,
    ctx: AppContext = Depends(get_ctx),
) -> JSONResponse:
    require_corpus(ctx, principal_of(request), corpus_id)
    if dry_run:
        data = api.PipelinePlan.model_validate(plan(ctx, corpus_id, body)).model_dump(mode="json")
        return JSONResponse(data, status_code=200)
    data = pipeline_out(ctx, create_pipeline(ctx, corpus_id, body)).model_dump(mode="json")
    return JSONResponse(data, status_code=201)


@router.get("/corpora/{corpus_id}/pipelines", response_model=api.Page[api.Pipeline])
def list_pipelines(
    corpus_id: str,
    request: Request,
    archived: bool = False,
    params: PageParams = Depends(page_params),
    ctx: AppContext = Depends(get_ctx),
) -> api.Page[api.Pipeline]:
    require_corpus(ctx, principal_of(request), corpus_id)
    rows = ctx.db.all(
        "SELECT id FROM pipelines WHERE corpus_id = ? AND archived = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (corpus_id, int(archived), params.limit + 1, params.offset),
    )
    items = [pipeline_out(ctx, r["id"]) for r in rows[: params.limit]]
    return api.Page[api.Pipeline](
        items=items, next_cursor=str(params.offset + params.limit) if len(rows) > params.limit else None
    )


@router.get("/pipelines/{pipeline_id}", response_model=api.Pipeline)
def get(pipeline_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.Pipeline:
    require_pipeline(ctx, principal_of(request), pipeline_id)
    return pipeline_out(ctx, pipeline_id)


@router.get("/pipelines/{pipeline_id}/status", response_model=api.PipelineStatus)
def get_status(pipeline_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.PipelineStatus:
    require_pipeline(ctx, principal_of(request), pipeline_id)
    return status(ctx, pipeline_id)


@router.get("/pipelines/{pipeline_id}/diff", response_model=api.PipelineDiff)
def diff(pipeline_id: str, against: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.PipelineDiff:
    require_pipeline(ctx, principal_of(request), pipeline_id)
    a = get_pipeline(ctx, pipeline_id)
    require_pipeline(ctx, principal_of(request), against)
    b = get_pipeline(ctx, against)
    if a["corpus_id"] != b["corpus_id"]:
        raise NotFound("Pipelines belong to different corpora.")
    return _diff(ctx, dict(a), dict(b))


@router.post("/pipelines/{pipeline_id}/retry-failed", response_model=api.RetryResult)
def retry_failed(pipeline_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.RetryResult:
    require_pipeline(ctx, principal_of(request), pipeline_id)
    pipeline = get_pipeline(ctx, pipeline_id)
    from ragcentre.timeutil import now_iso

    cur = ctx.db.execute(
        "UPDATE node_runs SET status='pending', attempt=0, error_code=NULL, error_message=NULL, run_after=? "
        "WHERE corpus_id = ? AND node_fp IN (SELECT node_fp FROM pipeline_nodes WHERE pipeline_id = ?) AND status = 'failed'",
        (now_iso(), pipeline["corpus_id"], pipeline_id),
    )
    return api.RetryResult(retried=cur.rowcount)


@router.post("/pipelines/{pipeline_id}/archive", response_model=api.Pipeline)
def archive(pipeline_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.Pipeline:
    require_pipeline(ctx, principal_of(request), pipeline_id)
    archive_pipeline(ctx, pipeline_id)
    return pipeline_out(ctx, pipeline_id)
