"""Health and administration endpoints."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Body, Depends

from ragcentre.api.deps import get_ctx
from ragcentre.context import AppContext
from ragcentre.db.migrate import current_version
from ragcentre.models.api import GcReport, GcRequest, Health
from ragcentre.pipeline.gc import collect

router = APIRouter(tags=["admin"])


@router.get("/health", response_model=Health)
def health(ctx: AppContext = Depends(get_ctx)) -> Health:
    return Health(
        status="ok",
        version=ctx.version,
        migration=current_version(ctx.db.conn),
        workers=ctx.worker_count,
        auth_required=bool(ctx.settings.api_token),
        secret_storage_enabled=bool(ctx.settings.secret_key),
    )


@router.post("/admin/gc", response_model=GcReport)
def garbage_collect(body: GcRequest | None = Body(None), ctx: AppContext = Depends(get_ctx)) -> GcReport:
    """Remove work, indexes and blobs no non-archived pipeline needs. A dry run by default."""
    result = collect(ctx, dry_run=True if body is None else body.dry_run)
    return GcReport.model_validate(asdict(result))
