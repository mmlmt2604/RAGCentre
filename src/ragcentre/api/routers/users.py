"""Session-only account and team membership administration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ragcentre.accounts import users
from ragcentre.api.authz import require_team
from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.api.permissions import SessionRoute
from ragcentre.api.routers.auth import require_session
from ragcentre.context import AppContext
from ragcentre.errors import Forbidden, NotFound
from ragcentre.models import api

router = APIRouter(route_class=SessionRoute, tags=["users"])


@router.get("/users", response_model=api.Page[api.User])
def list_users(
    request: Request, params: PageParams = Depends(page_params), ctx: AppContext = Depends(get_ctx)
) -> api.Page[api.User]:
    require_session(request, admin=True)
    rows = ctx.db.all("SELECT id FROM users ORDER BY username LIMIT ? OFFSET ?", (params.limit + 1, params.offset))
    part, nxt = params.from_overfetch(rows)
    return api.Page[api.User](
        items=[api.User.model_validate(users.user_out(ctx, r["id"])) for r in part], next_cursor=nxt
    )


@router.post("/users", response_model=api.UserSetupCreated, status_code=201)
def create_user(body: api.UserCreate, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.UserSetupCreated:
    p = require_session(request, admin=True)
    return api.UserSetupCreated.model_validate(
        users.create_user(ctx, body.username, body.display_name, actor=p.actor_id or "")
    )


@router.patch("/users/{user_id}", response_model=api.User)
def update_user(user_id: str, body: api.UserPatch, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.User:
    p = require_session(request, admin=True)
    return api.User.model_validate(
        users.patch_user(ctx, user_id, body.model_dump(exclude_none=True), actor=p.actor_id or "")
    )


@router.post("/users/{user_id}/setup-token", response_model=api.UserSetupCreated)
def setup_token(user_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.UserSetupCreated:
    p = require_session(request, admin=True)
    return api.UserSetupCreated.model_validate(users.reissue_setup(ctx, user_id, actor=p.actor_id or ""))


def _owner(request: Request, team_id: str) -> str:
    p = require_session(request)
    require_team(p, team_id)
    if not p.is_admin and p.role != "owner":
        raise Forbidden("Only team owners can manage memberships.")
    return p.actor_id or ""


def _member(ctx: AppContext, team_id: str, name: str) -> api.TeamMember:
    row = ctx.db.one(
        "SELECT u.id AS user_id, u.username, u.display_name, u.status, m.role FROM users u JOIN team_memberships m ON m.user_id=u.id WHERE m.team_id=? AND u.username=?",
        (team_id, users.username(name)),
    )
    if row is None:
        raise NotFound("Team member was not found.")
    return api.TeamMember.model_validate(dict(row))


@router.get("/teams/{team_id}/members", response_model=api.Page[api.TeamMember])
def members(
    team_id: str, request: Request, params: PageParams = Depends(page_params), ctx: AppContext = Depends(get_ctx)
) -> api.Page[api.TeamMember]:
    require_team(require_session(request), team_id)
    _owner(request, team_id)
    rows = ctx.db.all(
        "SELECT u.username FROM team_memberships m JOIN users u ON u.id=m.user_id WHERE m.team_id=? ORDER BY u.username LIMIT ? OFFSET ?",
        (team_id, params.limit + 1, params.offset),
    )
    part, nxt = params.from_overfetch(rows)
    return api.Page[api.TeamMember](items=[_member(ctx, team_id, r["username"]) for r in part], next_cursor=nxt)


@router.put("/teams/{team_id}/members/{username}", response_model=api.TeamMember)
def set_member(
    team_id: str, username: str, body: api.MembershipSet, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.TeamMember:
    require_team(require_session(request), team_id)
    actor = _owner(request, team_id)
    users.set_membership(ctx, team_id, username, body.role, actor=actor)
    return _member(ctx, team_id, username)


@router.delete("/teams/{team_id}/members/{username}", status_code=204)
def remove_member(team_id: str, username: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    require_team(require_session(request), team_id)
    actor = _owner(request, team_id)
    users.set_membership(ctx, team_id, username, None, actor=actor)
    return Response(status_code=204)
