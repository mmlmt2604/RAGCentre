"""Authentication: first-run setup, login, logout, password change, current user and CSRF."""

from __future__ import annotations

import ipaddress
import threading
import time
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from ragcentre.accounts import SESSION_COOKIE, normalize_username
from ragcentre.api.deps import get_ctx, get_principal, require_session
from ragcentre.context import AppContext
from ragcentre.errors import InvalidCredentials, RateLimited, SetupUnavailable
from ragcentre.models import api

router = APIRouter(tags=["auth"])

SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600


class _Limiter:
    """A small process-local fixed-window limiter for login attempts."""

    def __init__(self, max_attempts: int = 5, window_seconds: float = 60.0) -> None:
        self.max = max_attempts
        self.window = window_seconds
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, int]] = {}

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            start, count = self._buckets.get(key, (0.0, 0))
            return now - start > self.window or count < self.max

    def record(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            start, count = self._buckets.get(key, (now, 0))
            self._buckets[key] = (now, 1) if now - start > self.window else (start, count + 1)

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)


_limiter = _Limiter()
_verify_sem = threading.BoundedSemaphore(4)


def _me(user: Mapping[str, Any]) -> api.Me:
    return api.Me(
        id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        is_platform_admin=bool(user["is_platform_admin"]),
    )


def _setup_allowed(ctx: AppContext) -> bool:
    if ctx.settings.allow_remote_setup:
        return True
    host = ctx.settings.host
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        path="/",
        secure=request.url.scheme == "https",
        max_age=SESSION_MAX_AGE_SECONDS,
    )


@router.get("/auth/config", response_model=api.AuthConfig)
def auth_config(ctx: AppContext = Depends(get_ctx)) -> api.AuthConfig:
    has = ctx.accounts.has_accounts()
    token = ctx.settings.api_token is not None
    return api.AuthConfig(
        auth_required=has or token,
        setup_required=not has,
        login_available=has,
        setup_allowed=_setup_allowed(ctx),
    )


@router.get("/auth/whoami", response_model=api.WhoAmI)
def whoami(principal: dict[str, Any] = Depends(get_principal)) -> api.WhoAmI:
    return api.WhoAmI(scope=principal["scope"], kind=principal["kind"])


@router.post("/auth/setup", response_model=api.AuthSession, status_code=201)
def setup(
    body: api.SetupRequest, request: Request, response: Response, ctx: AppContext = Depends(get_ctx)
) -> api.AuthSession:
    if ctx.accounts.has_accounts():
        raise SetupUnavailable("An account already exists; sign in instead.")
    if not _setup_allowed(ctx):
        raise SetupUnavailable("First-run setup is unavailable on this bind; run 'ragcentre account init' locally.")
    user = ctx.accounts.create_account(body.username, body.display_name, body.password)
    token, csrf = ctx.accounts.create_session(user["id"], expected_auth_version=user["auth_version"])
    _set_session_cookie(response, request, token)
    return api.AuthSession(csrf=csrf, user=_me(user))


@router.post("/auth/login", response_model=api.AuthSession)
def login(
    body: api.LoginRequest, request: Request, response: Response, ctx: AppContext = Depends(get_ctx)
) -> api.AuthSession:
    username = normalize_username(body.username)
    addr = request.client.host if request.client else "local"
    keys = [f"u:{username}", f"a:{addr}"]
    for key in keys:
        if not _limiter.allowed(key):
            raise RateLimited("Too many login attempts; try again shortly.")
    with _verify_sem:
        user = ctx.accounts.verify_login(body.username, body.password)
    if user is None:
        for key in keys:
            _limiter.record(key)
        raise InvalidCredentials("Wrong username or password.")
    try:
        token, csrf = ctx.accounts.create_session(user["id"], expected_auth_version=user["auth_version"])
    except InvalidCredentials:
        for key in keys:
            _limiter.record(key)
        raise
    for key in keys:
        _limiter.reset(key)
    _set_session_cookie(response, request, token)
    return api.AuthSession(csrf=csrf, user=_me(user))


@router.post("/auth/logout", status_code=204)
def logout(
    request: Request, ctx: AppContext = Depends(get_ctx), _: dict[str, Any] = Depends(require_session)
) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        ctx.accounts.revoke_session(token)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.post("/auth/password", status_code=204)
def change_password(
    body: api.PasswordChangeRequest,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    principal: dict[str, Any] = Depends(require_session),
) -> Response:
    ctx.accounts.change_password(principal["user_id"], body.current_password, body.new_password)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/auth/me", response_model=api.Me)
def me(ctx: AppContext = Depends(get_ctx), _: dict[str, Any] = Depends(require_session)) -> api.Me:
    user = ctx.accounts.account()
    assert user is not None  # require_session guarantees a session, which implies an account
    return _me(user)


@router.get("/auth/csrf", response_model=api.CsrfToken)
def csrf(principal: dict[str, Any] = Depends(require_session)) -> api.CsrfToken:
    return api.CsrfToken(token=principal["csrf_secret"])
