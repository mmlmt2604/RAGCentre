"""Local account sessions and backwards-compatible bearer introspection."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ragcentre.accounts.http import check_origin, cookie_name
from ragcentre.accounts.sessions import local_enabled
from ragcentre.accounts.users import complete_setup
from ragcentre.api.authz import Principal, principal_of
from ragcentre.api.deps import get_ctx
from ragcentre.api.permissions import SessionRoute
from ragcentre.context import AppContext
from ragcentre.errors import Forbidden, UnsupportedMediaType
from ragcentre.models import api

router = APIRouter(route_class=SessionRoute, tags=["auth"])


def require_session(request: Request, *, admin: bool = False) -> Principal:
    p = principal_of(request)
    if p.actor_kind != "user" or (admin and not p.is_admin):
        raise Forbidden(
            "This action requires a local administrator session."
            if admin
            else "This action requires a local user session."
        )
    return p


def _local_json(ctx: AppContext, request: Request) -> None:
    if not local_enabled(ctx):
        raise Forbidden("Local accounts are not enabled.")
    check_origin(ctx.settings, request.headers, request.scope)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise UnsupportedMediaType("Use application/json for account requests.")


@router.get("/auth/config", response_model=api.AuthConfig)
def config(ctx: AppContext = Depends(get_ctx)) -> api.AuthConfig:
    enabled = local_enabled(ctx)
    return api.AuthConfig(
        mode="local" if enabled else "legacy",
        setup_required=enabled
        and ctx.db.scalar("SELECT 1 FROM users WHERE status='active' AND is_platform_admin=1") is None,
    )


@router.post("/auth/login", response_model=api.CurrentUser)
def login(
    body: api.LocalLogin, request: Request, response: Response, ctx: AppContext = Depends(get_ctx)
) -> api.CurrentUser:
    _local_json(ctx, request)
    token, session_id = ctx.sessions.login(
        body.username, body.password, request.client.host if request.client else "unknown"
    )
    response.set_cookie(
        cookie_name(ctx.settings),
        token,
        httponly=True,
        secure=not ctx.settings.insecure_local_cookie,
        samesite="lax",
        path="/",
        max_age=ctx.settings.session_lifetime_seconds,
    )
    session = ctx.db.one("SELECT user_id, csrf_token FROM user_sessions WHERE id=?", (session_id,))
    assert session is not None
    return api.CurrentUser.model_validate(ctx.sessions.current(session["user_id"], session["csrf_token"]))


@router.post("/auth/setup", status_code=204)
def setup(body: api.AccountSetup, request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    _local_json(ctx, request)
    ctx.sessions.limiter.check(f"setup:{request.client.host if request.client else 'unknown'}")
    complete_setup(ctx, body.token, body.password)
    return Response(status_code=204)


@router.get("/auth/me", response_model=api.CurrentUser)
def me(request: Request, ctx: AppContext = Depends(get_ctx)) -> api.CurrentUser:
    p = require_session(request)
    assert p.actor_id is not None and p.csrf_token is not None
    return api.CurrentUser.model_validate(ctx.sessions.current(p.actor_id, p.csrf_token))


@router.get("/auth/csrf", response_model=api.CsrfToken)
def csrf(request: Request) -> api.CsrfToken:
    p = require_session(request)
    return api.CsrfToken(csrf_token=p.csrf_token or "")


@router.post("/auth/logout", status_code=204)
def logout(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    p = require_session(request)
    assert p.session_id is not None and p.actor_id is not None
    ctx.sessions.logout(p.session_id, p.actor_id)
    response = Response(status_code=204)
    response.delete_cookie(
        cookie_name(ctx.settings),
        path="/",
        secure=not ctx.settings.insecure_local_cookie,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/auth/password", status_code=204)
def change_password(body: api.PasswordChange, request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    p = require_session(request)
    assert p.actor_id is not None
    ctx.sessions.change_password(
        p.actor_id, body.current_password, body.password, request.client.host if request.client else "unknown"
    )
    response = Response(status_code=204)
    response.delete_cookie(
        cookie_name(ctx.settings),
        path="/",
        secure=not ctx.settings.insecure_local_cookie,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/me/teams", response_model=api.Page[api.TeamMembership])
def my_teams(request: Request, ctx: AppContext = Depends(get_ctx)) -> api.Page[api.TeamMembership]:
    p = require_session(request)
    assert p.actor_id is not None
    current = ctx.sessions.current(p.actor_id, p.csrf_token or "")
    return api.Page[api.TeamMembership](
        items=[api.TeamMembership.model_validate(m) for m in current["memberships"]], next_cursor=None
    )


@router.get("/auth/whoami", response_model=api.WhoAmI)
def whoami(request: Request, ctx: AppContext = Depends(get_ctx)) -> api.WhoAmI:
    p = principal_of(request)
    team_name = None
    if p.team_id is not None:
        team_name = ctx.db.scalar("SELECT name FROM teams WHERE id = ?", (p.team_id,))
    return api.WhoAmI(scope=p.scope, team_id=p.team_id, team_name=team_name)
