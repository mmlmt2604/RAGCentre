"""Detection and readiness of OCR shims. None of these calls waits for a shim."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ragcentre.api.authz import principal_of, require_admin, require_provider
from ragcentre.api.deps import get_ctx
from ragcentre.api.permissions import SessionRoute
from ragcentre.context import AppContext
from ragcentre.models import api

router = APIRouter(route_class=SessionRoute, tags=["ocr"])


@router.get("/ocr-services", response_model=api.OcrStatus)
def get_ocr_services(request: Request, ctx: AppContext = Depends(get_ctx)) -> api.OcrStatus:
    """The OCR shims that are registered or were found, with their readiness. Poll it: each call
    starts the looks and checks that are due and returns what is known so far."""
    require_admin(principal_of(request))
    return api.OcrStatus.model_validate(ctx.ocr.snapshot())


@router.post("/ocr-services/scan", response_model=api.OcrStatus)
def scan_ocr_services(request: Request, ctx: AppContext = Depends(get_ctx)) -> api.OcrStatus:
    """Look at every known address now."""
    require_admin(principal_of(request))
    return api.OcrStatus.model_validate(ctx.ocr.scan())


@router.post("/providers/{provider_id}/ocr-check", response_model=api.OcrStatus, status_code=202)
def check_ocr(provider_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.OcrStatus:
    """Start reading a scanned test page with this parser. Poll ``GET /ocr-services`` for the result."""
    require_admin(principal_of(request))
    require_provider(ctx, principal_of(request), provider_id)
    return api.OcrStatus.model_validate(ctx.ocr.check(provider_id))
