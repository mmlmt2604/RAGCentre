"""FastAPI application factory. Every endpoint is a synchronous ``def``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ragcentre import __version__
from ragcentre.api.deps import require_token
from ragcentre.api.routers import ROUTERS
from ragcentre.api.ui import UiApp
from ragcentre.context import AppContext
from ragcentre.errors import ErrorCode, RagError
from ragcentre.logs import redact

log = logging.getLogger("ragcentre.api")

_STATUS_CODES = {
    400: ErrorCode.VALIDATION_ERROR,
    401: ErrorCode.UNAUTHORIZED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: ErrorCode.VALIDATION_ERROR,
}


def error_response(status: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": redact(message)}
    if details is not None:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status)


def _rag_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RagError)
    return error_response(exc.http_status, exc.error_code.value, exc.message, exc.details)


def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    problems = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body",))
        problems.append({"path": loc, "message": str(err.get("msg", ""))})
    summary = "; ".join(f"{p['path']}: {p['message']}" if p["path"] else p["message"] for p in problems[:5])
    return error_response(422, ErrorCode.VALIDATION_ERROR.value, f"Invalid request: {summary}", {"problems": problems})


def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code in (404, 405):
        # Unmatched routes skip dependencies; still refuse unauthenticated callers first.
        try:
            require_token(request)
        except RagError as auth:
            return _rag_error(request, auth)
    code = _STATUS_CODES.get(exc.status_code, ErrorCode.VALIDATION_ERROR)
    message = str(exc.detail) if exc.detail else "Request failed."
    if exc.status_code == 404 and message == "Not Found":
        message = "No such endpoint."
    if exc.status_code == 405:
        message = "Method not allowed on this endpoint."
    return error_response(exc.status_code, code.value, message)


def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error", exc_info=exc)
    return error_response(500, ErrorCode.INTERNAL_ERROR.value, "Internal server error.")


def create_app(ctx: AppContext, ui_dir: Path | None = None) -> FastAPI:
    """The API under ``/v1`` and the built web UI at ``/`` (``ui_dir`` overrides its location)."""
    app = FastAPI(
        title="RAGCentre",
        version=__version__,
        dependencies=[Depends(require_token)],
        docs_url="/v1/docs",
        openapi_url="/v1/openapi.json",
        redoc_url=None,
    )
    app.state.ctx = ctx
    app.add_exception_handler(RagError, _rag_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled)
    for router in ROUTERS:
        app.include_router(router, prefix="/v1")
    # Last, so every API route matches first. Mounts skip the app-level token dependency.
    app.mount("/", UiApp(ui_dir), name="ui")
    return app
