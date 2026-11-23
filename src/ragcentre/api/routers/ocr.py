"""Detection and readiness of OCR shims. None of these calls waits for a shim."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ragcentre.api.deps import get_ctx
from ragcentre.context import AppContext
from ragcentre.models import api

router = APIRouter(tags=["ocr"])


@router.get("/ocr-services", response_model=api.OcrStatus)
def get_ocr_services(ctx: AppContext = Depends(get_ctx)) -> api.OcrStatus:
    """The OCR shims that are registered or were found, with their readiness. Poll it: each call
    starts the looks and checks that are due and returns what is known so far."""
    return api.OcrStatus.model_validate(ctx.ocr.snapshot())


@router.post("/ocr-services/scan", response_model=api.OcrStatus)
def scan_ocr_services(ctx: AppContext = Depends(get_ctx)) -> api.OcrStatus:
    """Look at every known address now."""
    return api.OcrStatus.model_validate(ctx.ocr.scan())


@router.post("/providers/{provider_id}/ocr-check", response_model=api.OcrStatus, status_code=202)
def check_ocr(provider_id: str, ctx: AppContext = Depends(get_ctx)) -> api.OcrStatus:
    """Start reading a scanned test page with this parser. Poll ``GET /ocr-services`` for the result."""
    return api.OcrStatus.model_validate(ctx.ocr.check(provider_id))
