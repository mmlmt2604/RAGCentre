"""Node-run inspection and retry (section 16)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ragcentre.api.authz import principal_of, require_corpus, require_document, require_node_run, require_pipeline
from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.api.permissions import SessionRoute
from ragcentre.context import AppContext
from ragcentre.models import api
from ragcentre.pipeline.status import get_pipeline, run_out, run_state

router = APIRouter(route_class=SessionRoute, tags=["runs"])


@router.get("/node-runs", response_model=api.Page[api.NodeRun])
def list_runs(
    request: Request,
    corpus_id: str | None = None,
    document_id: str | None = None,
    pipeline_id: str | None = None,
    status: str | None = None,
    params: PageParams = Depends(page_params),
    ctx: AppContext = Depends(get_ctx),
) -> api.Page[api.NodeRun]:
    principal = principal_of(request)
    # Effective "blocked" states need Python, so pull candidate rows (bounded) and filter here.
    # The page cursor is only exact for unfiltered status; acceptable for the activity view.
    sql = "SELECT r.* FROM node_runs r WHERE 1=1"
    args: list[object] = []
    if corpus_id:
        require_corpus(ctx, principal, corpus_id)
        sql += " AND r.corpus_id = ?"
        args.append(corpus_id)
    if document_id:
        require_document(ctx, principal, document_id)
        sql += " AND r.document_id = ?"
        args.append(document_id)
    if pipeline_id:
        require_pipeline(ctx, principal, pipeline_id)
        pipeline = get_pipeline(ctx, pipeline_id)
        sql += " AND r.corpus_id = ? AND r.node_fp IN (SELECT node_fp FROM pipeline_nodes WHERE pipeline_id = ?)"
        args.extend([pipeline["corpus_id"], pipeline_id])
    if not principal.unrestricted and not corpus_id and not document_id and not pipeline_id:
        sql += " AND r.corpus_id IN (SELECT id FROM corpora WHERE team_id = ?)"
        args.append(principal.team_id)
    sql += " ORDER BY r.id DESC"
    rows = ctx.db.all(sql, (*args,))
    items = [run_out(ctx, row) for row in rows if status is None or run_state(ctx, row) == status]
    start = params.offset
    page = items[start : start + params.limit]
    return api.Page[api.NodeRun](
        items=page, next_cursor=str(start + params.limit) if start + params.limit < len(items) else None
    )


@router.post("/node-runs/{run_id}/retry", response_model=api.NodeRun)
def retry_run(run_id: int, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.NodeRun:
    from ragcentre.errors import NotFound
    from ragcentre.timeutil import now_iso

    require_node_run(ctx, principal_of(request), run_id)
    cur = ctx.db.execute(
        "UPDATE node_runs SET status='pending', attempt=0, error_code=NULL, error_message=NULL, run_after=? WHERE id = ?",
        (now_iso(), run_id),
    )
    if cur.rowcount == 0:
        raise NotFound(f"Node run {run_id} was not found.")
    row = ctx.db.one("SELECT * FROM node_runs WHERE id = ?", (run_id,))
    assert row is not None
    return run_out(ctx, row)
