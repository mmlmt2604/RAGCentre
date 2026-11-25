"""Evaluation endpoints (sections 14 and 16)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile

from ragcentre.api.authz import principal_of, require_corpus, require_dataset, require_eval_run
from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.context import AppContext
from ragcentre.errors import Conflict, NotFound, ValidationFailed
from ragcentre.eval import runner
from ragcentre.eval.datasets import import_dataset
from ragcentre.eval.datasets import items as dataset_items
from ragcentre.models import api

router = APIRouter(tags=["eval"])

MAX_DATASET_BYTES = 50 * 1024 * 1024


def _dataset_out(ctx: AppContext, dataset_id: str) -> api.EvalDataset:
    row = ctx.db.one(
        "SELECT d.*, (SELECT COUNT(*) FROM eval_items i WHERE i.dataset_id = d.id) AS item_count "
        "FROM eval_datasets d WHERE d.id = ?",
        (dataset_id,),
    )
    if row is None:
        raise NotFound(f"Dataset {dataset_id} was not found.")
    return api.EvalDataset(
        id=row["id"], corpus_id=row["corpus_id"], name=row["name"], item_count=row["item_count"],
        created_at=row["created_at"],
    )  # fmt: skip


@router.post("/corpora/{corpus_id}/eval/datasets", response_model=api.EvalDataset, status_code=201)
def upload_dataset(
    corpus_id: str,
    request: Request,
    name: str = Form(...),
    file: UploadFile = File(...),
    ctx: AppContext = Depends(get_ctx),
) -> api.EvalDataset:
    require_corpus(ctx, principal_of(request), corpus_id)
    raw = file.file.read(MAX_DATASET_BYTES + 1)
    if len(raw) > MAX_DATASET_BYTES:
        raise ValidationFailed("The dataset file is larger than 50 MB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationFailed("The dataset must be UTF-8 JSON Lines.") from None
    return _dataset_out(ctx, import_dataset(ctx, corpus_id, name, text))


@router.get("/corpora/{corpus_id}/eval/datasets", response_model=api.Page[api.EvalDataset])
def list_datasets(
    corpus_id: str, request: Request, params: PageParams = Depends(page_params), ctx: AppContext = Depends(get_ctx)
) -> api.Page[api.EvalDataset]:
    require_corpus(ctx, principal_of(request), corpus_id)
    rows = ctx.db.all(
        "SELECT id FROM eval_datasets WHERE corpus_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
        (corpus_id, params.limit + 1, params.offset),
    )
    part, nxt = params.from_overfetch(rows)
    return api.Page[api.EvalDataset](items=[_dataset_out(ctx, r["id"]) for r in part], next_cursor=nxt)


@router.get("/eval/datasets/{dataset_id}", response_model=api.EvalDataset)
def get_dataset(dataset_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.EvalDataset:
    require_dataset(ctx, principal_of(request), dataset_id)
    return _dataset_out(ctx, dataset_id)


@router.get("/eval/datasets/{dataset_id}/items", response_model=api.Page[api.EvalItem])
def get_dataset_items(
    dataset_id: str,
    request: Request,
    params: PageParams = Depends(page_params),
    ctx: AppContext = Depends(get_ctx),
) -> api.Page[api.EvalItem]:
    require_dataset(ctx, principal_of(request), dataset_id)
    _dataset_out(ctx, dataset_id)
    part, nxt = params.page(dataset_items(ctx, dataset_id))
    return api.Page[api.EvalItem](items=[api.EvalItem.model_validate(i) for i in part], next_cursor=nxt)


@router.delete("/eval/datasets/{dataset_id}", status_code=204)
def delete_dataset(dataset_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    require_dataset(ctx, principal_of(request), dataset_id)
    _dataset_out(ctx, dataset_id)
    with ctx.db.write() as conn:
        busy = conn.execute(
            "SELECT 1 FROM eval_runs WHERE dataset_id = ? AND status IN ('pending', 'running')", (dataset_id,)
        ).fetchone()
        if busy is not None:
            raise Conflict("An evaluation run on this dataset is still in progress.")
        runs = "SELECT id FROM eval_runs WHERE dataset_id = ?"
        conn.execute(f"DELETE FROM eval_results WHERE run_id IN ({runs})", (dataset_id,))
        conn.execute(f"DELETE FROM eval_summaries WHERE run_id IN ({runs})", (dataset_id,))
        conn.execute("DELETE FROM eval_runs WHERE dataset_id = ?", (dataset_id,))
        conn.execute("DELETE FROM eval_datasets WHERE id = ?", (dataset_id,))
    return Response(status_code=204)


@router.get("/corpora/{corpus_id}/eval/runs", response_model=api.Page[api.EvalRun])
def list_runs(
    corpus_id: str, request: Request, params: PageParams = Depends(page_params), ctx: AppContext = Depends(get_ctx)
) -> api.Page[api.EvalRun]:
    require_corpus(ctx, principal_of(request), corpus_id)
    rows = ctx.db.all(
        "SELECT r.id FROM eval_runs r JOIN eval_datasets d ON d.id = r.dataset_id WHERE d.corpus_id = ? "
        "ORDER BY r.created_at DESC, r.rowid DESC LIMIT ? OFFSET ?",
        (corpus_id, params.limit + 1, params.offset),
    )
    part, nxt = params.from_overfetch(rows)
    return api.Page[api.EvalRun](
        items=[api.EvalRun.model_validate(runner.run_out(ctx, r["id"])) for r in part], next_cursor=nxt
    )


@router.post("/eval/runs", response_model=api.EvalRun, status_code=201)
def create_run(body: api.EvalRunCreate, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.EvalRun:
    require_dataset(ctx, principal_of(request), body.dataset_id)
    run_id = runner.create_run(ctx, body.dataset_id, body.pipeline_ids, body.k_values, body.allow_partial)
    return api.EvalRun.model_validate(runner.run_out(ctx, run_id))


@router.get("/eval/runs/{run_id}", response_model=api.EvalRunDetail)
def get_run(run_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.EvalRunDetail:
    require_eval_run(ctx, principal_of(request), run_id)
    return api.EvalRunDetail.model_validate(runner.run_detail(ctx, run_id))


@router.get("/eval/runs/{run_id}/items", response_model=api.Page[api.EvalItemComparison])
def get_run_items(
    run_id: str,
    a: str,
    b: str,
    request: Request,
    metric: str = "mrr",
    only: str | None = None,
    params: PageParams = Depends(page_params),
    ctx: AppContext = Depends(get_ctx),
) -> api.Page[api.EvalItemComparison]:
    require_eval_run(ctx, principal_of(request), run_id)
    part, nxt = params.page(runner.item_comparisons(ctx, run_id, a, b, metric, only))
    return api.Page[api.EvalItemComparison](
        items=[api.EvalItemComparison.model_validate(x) for x in part], next_cursor=nxt
    )
