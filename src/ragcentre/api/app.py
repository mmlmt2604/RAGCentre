"""FastAPI application factory. Every endpoint is a synchronous ``def``."""

from __future__ import annotations

import hashlib
import hmac
import logging
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette._utils import get_route_path
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ragcentre import __version__
from ragcentre.accounts.http import check_csrf, cookie_name, validate_browser_settings
from ragcentre.accounts.sessions import local_enabled
from ragcentre.api.authz import Principal, read_allowed
from ragcentre.api.routers import ROUTERS
from ragcentre.api.ui import UiApp, is_api_path
from ragcentre.context import AppContext
from ragcentre.errors import ErrorCode, NotFound, RagError
from ragcentre.logs import redact
from ragcentre.timeutil import now_iso

log = logging.getLogger("ragcentre.api")

_STATUS_CODES = {
    400: ErrorCode.VALIDATION_ERROR,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    429: ErrorCode.RATE_LIMITED,
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
    response = error_response(exc.http_status, exc.error_code.value, exc.message, exc.details)
    if exc.http_status == 429:
        response.headers["Retry-After"] = "60"
    return response


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

    The result is a ``Principal`` stored on ``request.state.principal``. A read-scoped team token
    is additionally restricted to the read-only allowlist here, before any handler runs.
    """

    def __init__(self, app: ASGIApp, ctx: AppContext) -> None:
        self.app = app
        self.ctx = ctx
        self.token = ctx.settings.api_token.encode() if ctx.settings.api_token else None
        self.limit = ctx.settings.max_upload_bytes

    @staticmethod
    def _is_public(scope: Scope) -> bool:
        path = get_route_path(scope)
        raw_path = scope.get("raw_path", b"").lower()
        if "//" in path or "\\" in path or {".", ".."}.intersection(path.split("/")) or b"%2f" in raw_path:
            return False
        if is_api_path(path):
            return (scope["method"], path) in {
                ("GET", "/v1/health"),
                ("GET", "/v1/auth/config"),
                ("POST", "/v1/auth/login"),
                ("POST", "/v1/auth/setup"),
            }
        # Every API route is under /v1. Read-only paths outside it serve the built UI.
        return bool(path.startswith("/") and scope["method"] in {"GET", "HEAD"})

    def _authenticate(self, headers: Headers) -> Principal | None:
        scheme, _, supplied = headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and supplied.strip():
            raw = supplied.strip().encode()
            if self.token is not None and hmac.compare_digest(raw, self.token):
                return Principal("admin", None, actor_kind="admin_token")
            token_hash = hashlib.sha256(raw).hexdigest()
            row = self.ctx.db.one("SELECT team_id, kind FROM team_tokens WHERE token_hash = ?", (token_hash,))
            if row is not None:
                self.ctx.db.execute(
                    "UPDATE team_tokens SET last_used_at = ? WHERE token_hash = ?", (now_iso(), token_hash)
                )
                # DB stores the token's write capability ('full' | 'read'); the principal models
                # it as a scope ('team' | 'read'). 'team' is the full team scope.
                scope: Literal["team", "read"] = "team" if row["kind"] == "full" else "read"
                selected = headers.get("x-ragcentre-team-id")
                if selected is not None and selected != row["team_id"]:
                    raise NotFound("Team was not found.")
                return Principal(scope, str(row["team_id"]), actor_kind="team_token", actor_id=token_hash)
        if "authorization" in headers:
            return None
        if local_enabled(self.ctx):
            cookies = SimpleCookie()
            try:
                cookies.load(headers.get("cookie", ""))
            except CookieError:
                return None
            credential = cookies.get(cookie_name(self.ctx.settings))
            session = self.ctx.sessions.resolve(credential.value) if credential else None
            if session is None:
                return None
            selected = headers.get("x-ragcentre-team-id")
            role = None
            if selected:
                membership = self.ctx.db.one(
                    "SELECT role FROM team_memberships WHERE user_id=? AND team_id=?", (session["user_id"], selected)
                )
                if membership:
                    role = membership["role"]
                elif (
                    not session["is_platform_admin"]
                    or self.ctx.db.scalar("SELECT 1 FROM teams WHERE id=?", (selected,)) is None
                ):
                    raise NotFound("Team was not found.")
            return Principal(
                "admin" if session["is_platform_admin"] else "team",
                selected,
                actor_kind="user",
                actor_id=session["user_id"],
                session_id=session["id"],
                csrf_token=session["csrf_token"],
                role=role,
            )
        if self.token is None and self.ctx.db.scalar("SELECT 1 FROM team_tokens LIMIT 1") is None:
            return Principal("admin", None, actor_kind="development")
        return None

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
                    headers.setdefault("Cache-Control", "no-store")
                    headers.setdefault("Referrer-Policy", "no-referrer")
                    policy = "sandbox"
                    if get_route_path(scope) in {"/v1/docs", "/v1/docs/oauth2-redirect"}:
                        policy += " allow-scripts allow-same-origin"
                    headers.setdefault("Content-Security-Policy", policy)
                await original_send(message)

            send = secured_send

        headers = Headers(scope=scope)
        if not self._is_public(scope):
            try:
                principal = self._authenticate(headers)
                if (
                    principal is not None
                    and principal.actor_kind == "user"
                    and scope["method"] not in {"GET", "HEAD", "OPTIONS"}
                ):
                    check_csrf(self.ctx.settings, headers, scope, principal.csrf_token or "")
            except RagError as exc:
                await error_response(exc.http_status, exc.error_code.value, exc.message)(scope, receive, send)
                return
            if principal is None:
                response = error_response(
                    401,
                    ErrorCode.UNAUTHORIZED.value,
                    "This server requires an access token. Send it as 'Authorization: Bearer <token>'.",
                )
                response.headers["WWW-Authenticate"] = "Bearer"
                await response(scope, receive, send)
                return
            if principal.scope == "read" and not read_allowed(scope["method"], get_route_path(scope)):
                await error_response(
                    403, ErrorCode.READ_ONLY_TOKEN.value, "This token is read-only and cannot perform this action."
                )(scope, receive, send)
                return
            scope.setdefault("state", {})["principal"] = principal

        body_limit = min(self.limit, 16384) if get_route_path(scope).startswith("/v1/auth/") else self.limit
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
            if len(digits) > len(str(body_limit)) or int(digits) > body_limit:
                await error_response(413, ErrorCode.PAYLOAD_TOO_LARGE.value, too_large)(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > body_limit:
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
        if local_enabled(ctx):
            validate_browser_settings(ctx.settings)
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
