"""Health and administration endpoints."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Body, Depends

from ragcentre.api.deps import get_ctx
from ragcentre.context import AppContext
from ragcentre.errors import ValidationFailed
from ragcentre.models.api import CacheClearReport, CacheClearRequest, GcReport, GcRequest, Health
from ragcentre.pipeline.gc import collect

router = APIRouter(tags=["admin"])


@router.get("/health", response_model=Health)
def health(ctx: AppContext = Depends(get_ctx)) -> Health:
    return Health(
        status="ok",
        version=ctx.version,
        migration=int(ctx.db.scalar("PRAGMA user_version")),
        workers=ctx.worker_count,
        auth_required=bool(ctx.settings.api_token),
        secret_storage_enabled=bool(ctx.settings.secret_key),
    )


@router.post("/admin/gc", response_model=GcReport)
def garbage_collect(body: GcRequest | None = Body(None), ctx: AppContext = Depends(get_ctx)) -> GcReport:
    """Remove work, indexes and blobs no non-archived pipeline needs. A dry run by default."""
    result = collect(ctx, dry_run=True if body is None else body.dry_run)
    return GcReport.model_validate(asdict(result))


@router.post("/admin/cache/clear", response_model=CacheClearReport)
def clear_cache(body: CacheClearRequest, ctx: AppContext = Depends(get_ctx)) -> CacheClearReport:
    """Empty a cache. Clear the query-transform cache after a model changes behind the same settings."""
    if not body.transforms:
        raise ValidationFailed("Choose what to clear: set transforms to true.")
    return CacheClearReport(transforms_deleted=ctx.cache.execute("DELETE FROM query_transforms").rowcount)
