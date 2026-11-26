"""Human-account invitation, recovery and operator-claim endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ragcentre.accounts import bootstrap, invitations, recovery
from ragcentre.accounts.credentials import authority
from ragcentre.accounts.http import cookie_name
from ragcentre.api.authz import require_team
from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.api.permissions import SessionRoute
from ragcentre.api.routers.auth import _local_json, require_session
from ragcentre.context import AppContext
from ragcentre.errors import Forbidden
from ragcentre.models import api

router = APIRouter(route_class=SessionRoute, tags=["account onboarding"])


def _public(ctx: AppContext, request: Request) -> None:
    _local_json(ctx, request)
    if "authorization" in request.headers:
        raise Forbidden("Use a purpose-specific account link without bearer authorization.")
    peer = request.client.host if request.client else "unknown"
    ctx.sessions.limiter.check(f"account-link:{peer}")


def _browser_user(ctx: AppContext, request: Request) -> str | None:
    token = request.cookies.get(cookie_name(ctx.settings))
    session = ctx.sessions.resolve(token) if token else None
    return session["user_id"] if session else None


def _arrival(ctx: AppContext, response: Response, token: str, session_id: str, team_id: str) -> api.AccountArrival:
    session = ctx.db.one("SELECT user_id,csrf_token FROM user_sessions WHERE id=?", (session_id,))
    assert session is not None
    response.set_cookie(
        cookie_name(ctx.settings),
        token,
        httponly=True,
        secure=not ctx.settings.insecure_local_cookie,
        samesite="lax",
        path="/",
        max_age=ctx.settings.session_lifetime_seconds,
    )
    return api.AccountArrival(
        current=api.CurrentUser.model_validate(ctx.sessions.current(session["user_id"], session["csrf_token"])),
        team_id=team_id,
    )


@router.get("/teams/{team_id}/invitations", response_model=api.Page[api.TeamInvitation])
def list_invitations(
    team_id: str, request: Request, params: PageParams = Depends(page_params), ctx: AppContext = Depends(get_ctx)
) -> api.Page[api.TeamInvitation]:
    p = require_session(request)
    require_team(p, team_id)
    assert p.actor_id is not None
    with ctx.db.read() as conn:
        authority(conn, p.actor_id, team_id)
        rows = conn.execute(
            "SELECT id FROM team_invitations WHERE team_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
            (team_id, params.limit + 1, params.offset),
        ).fetchall()
        part, nxt = params.from_overfetch(rows)
        items = [
            api.TeamInvitation.model_validate(invitations.invitation_out(conn, row["id"], team_id)) for row in part
        ]
    return api.Page[api.TeamInvitation](items=items, next_cursor=nxt)


@router.post("/teams/{team_id}/invitations", response_model=api.InvitationCreated, status_code=201)
def issue_invitation(
    team_id: str, body: api.InvitationCreate, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.InvitationCreated:
    p = require_session(request)
    require_team(p, team_id)
    assert p.actor_id is not None
    return api.InvitationCreated.model_validate(
        invitations.issue(ctx, team_id, body.username, body.display_name, body.role, actor=p.actor_id)
    )


@router.post("/teams/{team_id}/invitations/{invitation_id}/reissue", response_model=api.InvitationCreated)
def reissue_invitation(
    team_id: str, invitation_id: str, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.InvitationCreated:
    p = require_session(request)
    require_team(p, team_id)
    assert p.actor_id is not None
    return api.InvitationCreated.model_validate(invitations.reissue(ctx, team_id, invitation_id, actor=p.actor_id))


@router.delete("/teams/{team_id}/invitations/{invitation_id}", status_code=204)
def revoke_invitation(
    team_id: str, invitation_id: str, request: Request, ctx: AppContext = Depends(get_ctx)
) -> Response:
    p = require_session(request)
    require_team(p, team_id)
    assert p.actor_id is not None
    invitations.revoke(ctx, team_id, invitation_id, actor=p.actor_id)
    return Response(status_code=204)


@router.post("/auth/invitations/inspect", response_model=api.InvitationInspection)
def inspect_invitation(
    body: api.PurposeToken, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.InvitationInspection:
    _public(ctx, request)
    return api.InvitationInspection.model_validate(invitations.inspect(ctx, body.token))


@router.post("/auth/invitations/activate", response_model=api.AccountArrival)
def activate_invitation(
    body: api.InvitationActivation, request: Request, response: Response, ctx: AppContext = Depends(get_ctx)
) -> api.AccountArrival:
    _public(ctx, request)
    token, session_id, team_id = invitations.activate(
        ctx, body.token, body.password, body.display_name, browser_user_id=_browser_user(ctx, request)
    )
    return _arrival(ctx, response, token, session_id, team_id)


@router.post("/auth/invitations/accept", response_model=api.AccountArrival)
def accept_invitation(
    body: api.PurposeToken, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.AccountArrival:
    p = require_session(request)
    assert p.actor_id is not None and p.session_id is not None and p.csrf_token is not None
    team_id, existing = invitations.accept(ctx, body.token, actor=p.actor_id, session_id=p.session_id)
    return api.AccountArrival(
        current=api.CurrentUser.model_validate(ctx.sessions.current(p.actor_id, p.csrf_token)),
        team_id=team_id,
        already_member=existing,
    )


@router.post("/users/{user_id}/recovery-token", response_model=api.AccountLinkCreated)
def issue_recovery(
    user_id: str, body: api.RecoveryIssue, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.AccountLinkCreated:
    p = require_session(request, admin=True)
    assert p.actor_id is not None and p.session_id is not None
    return api.AccountLinkCreated.model_validate(
        recovery.issue(
            ctx,
            user_id,
            body.current_password,
            actor=p.actor_id,
            session_id=p.session_id,
            peer=request.client.host if request.client else "unknown",
        )
    )


@router.post("/auth/recovery/inspect", response_model=api.RecoveryInspection)
def inspect_recovery(
    body: api.PurposeToken, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.RecoveryInspection:
    _public(ctx, request)
    return api.RecoveryInspection.model_validate(recovery.inspect(ctx, body.token))


@router.post("/auth/recovery/complete", status_code=204)
def complete_recovery(body: api.AccountSetup, request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    _public(ctx, request)
    recovery.complete(ctx, body.token, body.password)
    return Response(status_code=204)


@router.post("/auth/bootstrap/inspect", response_model=api.BootstrapInspection)
def inspect_bootstrap(
    body: api.PurposeToken, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.BootstrapInspection:
    _public(ctx, request)
    return api.BootstrapInspection.model_validate(bootstrap.inspect(ctx, body.token))


@router.post("/auth/bootstrap/complete", response_model=api.AccountArrival)
def complete_bootstrap(
    body: api.BootstrapComplete, request: Request, response: Response, ctx: AppContext = Depends(get_ctx)
) -> api.AccountArrival:
    _public(ctx, request)
    token, session_id, team_id = bootstrap.complete(
        ctx,
        body.token,
        body.username,
        body.password,
        body.display_name,
        body.team_name,
        browser_user_id=_browser_user(ctx, request),
    )
    return _arrival(ctx, response, token, session_id, team_id)
