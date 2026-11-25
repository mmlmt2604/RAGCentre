"""Health and administration endpoints."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Body, Depends, Request

from ragcentre.accounts.sessions import local_enabled
from ragcentre.api.authz import principal_of, require_admin
from ragcentre.api.deps import get_ctx
from ragcentre.api.permissions import SessionRoute
from ragcentre.context import AppContext
from ragcentre.errors import ValidationFailed
from ragcentre.models.api import CacheClearReport, CacheClearRequest, GcReport, GcRequest, Health
from ragcentre.pipeline.gc import collect

router = APIRouter(route_class=SessionRoute, tags=["admin"])


@router.get("/health", response_model=Health)
def health(ctx: AppContext = Depends(get_ctx)) -> Health:
    return Health(
        status="ok",
        version=ctx.version,
        migration=int(ctx.db.scalar("PRAGMA user_version")),
        workers=ctx.worker_count,
        auth_required=local_enabled(ctx)
        or bool(ctx.settings.api_token)
        or ctx.db.scalar("SELECT 1 FROM team_tokens LIMIT 1") is not None,
        secret_storage_enabled=bool(ctx.settings.secret_key),
    )


@router.post("/admin/gc", response_model=GcReport)
def garbage_collect(
    request: Request, body: GcRequest | None = Body(None), ctx: AppContext = Depends(get_ctx)
) -> GcReport:
    """Remove work, indexes and blobs no non-archived pipeline needs. A dry run by default."""
    require_admin(principal_of(request))
    result = collect(ctx, dry_run=True if body is None else body.dry_run)
    return GcReport.model_validate(asdict(result))


@router.post("/admin/cache/clear", response_model=CacheClearReport)
def clear_cache(body: CacheClearRequest, request: Request, ctx: AppContext = Depends(get_ctx)) -> CacheClearReport:
    """Empty a cache. Clear the query-transform cache after a model changes behind the same settings."""
    require_admin(principal_of(request))
    if not body.transforms:
        raise ValidationFailed("Choose what to clear: set transforms to true.")
    return CacheClearReport(transforms_deleted=ctx.cache.execute("DELETE FROM query_transforms").rowcount)
