"""FastAPI application factory. Every endpoint is a synchronous ``def``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette._utils import get_route_path
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ragcentre import __version__
from ragcentre.api.principal import NONE, READ, csrf_valid, read_scope_allows, resolve_principal
from ragcentre.api.routers import ROUTERS
from ragcentre.api.ui import UiApp, is_api_path
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
    return JSONResponse(
        {"error": body},
        status_code=status,
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
    )


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


def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
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


class RequestGuard:
    """Authenticate before routing and bound the body before request parsing.

    Resolves one of three credential types to a principal: the legacy ``RAGCENTRE_API_TOKEN``
    (full), a named API key (read or full), or a browser session cookie (full). A read-scoped key is
    confined to the read allowlist in :mod:`ragcentre.api.principal`.
    """

    def __init__(self, app: ASGIApp, ctx: AppContext) -> None:
        self.app = app
        self.ctx = ctx
        self.limit = ctx.settings.max_upload_bytes

    @staticmethod
    def _is_public(scope: Scope) -> bool:
        path = get_route_path(scope)
        raw_path = scope.get("raw_path", b"").lower()
        if "//" in path or "\\" in path or {".", ".."}.intersection(path.split("/")) or b"%2f" in raw_path:
            return False
        if is_api_path(path):
            method = scope["method"]
            if method == "GET" and path in {"/v1/health", "/v1/auth/config", "/v1/auth/whoami"}:
                return True
            return method == "POST" and path in {"/v1/auth/login", "/v1/auth/setup"}
        # Every API route is under /v1. Read-only paths outside it serve the built UI.
        return bool(path.startswith("/") and scope["method"] in {"GET", "HEAD"})

    def _auth_required(self) -> bool:
        return self.ctx.settings.api_token is not None or self.ctx.accounts.has_accounts()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if is_api_path(get_route_path(scope)):
            original_send = send

            async def secured_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    headers.setdefault("X-Content-Type-Options", "nosniff")
                    policy = "sandbox"
                    if get_route_path(scope) in {"/v1/docs", "/v1/docs/oauth2-redirect"}:
                        policy += " allow-scripts allow-same-origin"
                    headers.setdefault("Content-Security-Policy", policy)
                await original_send(message)

            send = secured_send

        headers = Headers(scope=scope)
        path = get_route_path(scope)
        principal = resolve_principal(self.ctx, headers)
        scope["ragcentre_principal"] = principal
        public = self._is_public(scope)

        if not public and self._auth_required():
            if principal["scope"] == NONE:
                response = error_response(
                    401,
                    ErrorCode.UNAUTHORIZED.value,
                    "Authentication required. Send 'Authorization: Bearer <key>' or sign in.",
                )
                response.headers["WWW-Authenticate"] = "Bearer"
                await response(scope, receive, send)
                return
            if principal["scope"] == READ and not read_scope_allows(scope["method"], path):
                await error_response(
                    403, ErrorCode.READ_ONLY_TOKEN.value, "This key can read but cannot access this route."
                )(scope, receive, send)
                return

        if (
            not public
            and principal["kind"] == "session"
            and scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
            and is_api_path(path)
            and not csrf_valid(headers, principal)
        ):
            await error_response(403, ErrorCode.CSRF_INVALID.value, "Missing or invalid CSRF token.")(
                scope, receive, send
            )
            return

        too_large = "Request body exceeds the upload limit."
        lengths = headers.getlist("content-length")
        if lengths:
            length = lengths[0]
            if len(lengths) != 1 or not (length.isascii() and length.isdecimal()):
                await error_response(400, ErrorCode.VALIDATION_ERROR.value, "Invalid Content-Length.")(
                    scope, receive, send
                )
                return
            digits = length.lstrip("0") or "0"
            if len(digits) > len(str(self.limit)) or int(digits) > self.limit:
                await error_response(413, ErrorCode.PAYLOAD_TOO_LARGE.value, too_large)(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise StarletteHTTPException(413, too_large)
            return message

        await self.app(scope, limited_receive, send)


def create_app(ctx: AppContext | None, ui_dir: Path | None = None) -> FastAPI:
    """The API under ``/v1`` and the built web UI at ``/`` (``ui_dir`` overrides its location)."""
    app = FastAPI(
        title="RAGCentre",
        version=__version__,
        docs_url="/v1/docs",
        swagger_ui_oauth2_redirect_url="/v1/docs/oauth2-redirect",
        openapi_url="/v1/openapi.json",
        redoc_url=None,
    )
    app.state.ctx = ctx
    if ctx is not None:
        app.add_middleware(RequestGuard, ctx=ctx)
    app.add_exception_handler(RagError, _rag_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled)
    for router in ROUTERS:
        app.include_router(router, prefix="/v1")
    # Last, so every API route matches first.
    app.mount("/", UiApp(ui_dir), name="ui")
    return app
