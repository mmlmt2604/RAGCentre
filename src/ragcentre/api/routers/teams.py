"""Teams and team service tokens."""

from __future__ import annotations

import re
import secrets as _secrets
import sqlite3

from fastapi import APIRouter, Depends, Request, Response

from ragcentre.api.authz import principal_of, require_admin, require_team
from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.api.permissions import SessionRoute
from ragcentre.context import AppContext
from ragcentre.errors import Conflict, NotFound, ValidationFailed
from ragcentre.hashing import new_id, sha256_text
from ragcentre.models import api
from ragcentre.timeutil import now_iso

router = APIRouter(route_class=SessionRoute, tags=["teams"])

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug


def _team_out(ctx: AppContext, team_id: str) -> api.Team:
    row = ctx.db.one("SELECT * FROM teams WHERE id = ?", (team_id,))
    if row is None:
        raise NotFound(f"Team {team_id} was not found.")
    return api.Team(id=row["id"], name=row["name"], slug=row["slug"], created_at=row["created_at"])


def _token_out(ctx: AppContext, token_id: str) -> api.TeamToken:
    row = ctx.db.one("SELECT * FROM team_tokens WHERE id = ?", (token_id,))
    if row is None:
        raise NotFound(f"Team token {token_id} was not found.")
    return api.TeamToken(
        id=row["id"],
        team_id=row["team_id"],
        kind=row["kind"],
        name=row["name"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


@router.get("/teams", response_model=api.Page[api.Team])
def list_teams(
    request: Request, params: PageParams = Depends(page_params), ctx: AppContext = Depends(get_ctx)
) -> api.Page[api.Team]:
    require_admin(principal_of(request))
    rows = ctx.db.all("SELECT id FROM teams ORDER BY created_at LIMIT ? OFFSET ?", (params.limit + 1, params.offset))
    part, nxt = params.from_overfetch(rows)
    return api.Page[api.Team](items=[_team_out(ctx, r["id"]) for r in part], next_cursor=nxt)


@router.post("/teams", response_model=api.Team, status_code=201)
def create_team(request: Request, body: api.TeamCreate, ctx: AppContext = Depends(get_ctx)) -> api.Team:
    require_admin(principal_of(request))
    name = body.name.strip()
    if not name:
        raise ValidationFailed("Give the team a name.")
    slug = body.slug or _slugify(name)
    if not _SLUG_RE.fullmatch(slug):
        raise ValidationFailed(
            "Team slugs use lowercase letters, digits and hyphens, start with a letter, and are at most 63 characters."
        )
    team_id = new_id()
    try:
        with ctx.db.write() as conn:
            p = principal_of(request)
            if p.actor_kind == "user" and body.owner_user_id is None:
                raise ValidationFailed("Choose an active initial owner for the new team.")
            if body.owner_user_id is not None and p.actor_kind != "user":
                raise ValidationFailed("Assign account ownership through a local administrator session.")
            if body.owner_user_id is not None:
                owner = conn.execute(
                    "SELECT id FROM users WHERE id=? AND status='active'", (body.owner_user_id,)
                ).fetchone()
                if owner is None:
                    raise ValidationFailed("The initial owner must be an active user.")
            now = now_iso()
            conn.execute(
                "INSERT INTO teams (id, name, slug, created_at) VALUES (?, ?, ?, ?)", (team_id, name, slug, now)
            )
            if body.owner_user_id is not None:
                conn.execute(
                    "INSERT INTO team_memberships VALUES (?, ?, 'owner', ?, ?)", (team_id, body.owner_user_id, now, now)
                )
    except sqlite3.IntegrityError:
        raise Conflict(f"A team with slug {slug!r} already exists.") from None
    return _team_out(ctx, team_id)


@router.get("/teams/{team_id}", response_model=api.Team)
def get_team(team_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.Team:
    require_team(principal_of(request), team_id)
    return _team_out(ctx, team_id)


@router.post("/teams/{team_id}/tokens", response_model=api.TeamTokenCreated, status_code=201)
def create_token(
    team_id: str, request: Request, body: api.TeamTokenCreate, ctx: AppContext = Depends(get_ctx)
) -> api.TeamTokenCreated:
    require_team(principal_of(request), team_id)
    _team_out(ctx, team_id)  # 404 for an unknown team
    name = body.name.strip()
    if not name:
        raise ValidationFailed("Give the token a name.")
    raw = f"rct_{_secrets.token_urlsafe(32)}"
    token_hash = sha256_text(raw)
    token_id = new_id()
    now = now_iso()
    ctx.db.execute(
        "INSERT INTO team_tokens (id, team_id, kind, name, token_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (token_id, team_id, body.kind, name, token_hash, now),
    )
    return api.TeamTokenCreated(id=token_id, team_id=team_id, kind=body.kind, name=name, token=raw, created_at=now)


@router.get("/teams/{team_id}/tokens", response_model=api.Page[api.TeamToken])
def list_tokens(
    team_id: str,
    request: Request,
    params: PageParams = Depends(page_params),
    ctx: AppContext = Depends(get_ctx),
) -> api.Page[api.TeamToken]:
    require_team(principal_of(request), team_id)
    rows = ctx.db.all(
        "SELECT id FROM team_tokens WHERE team_id = ? ORDER BY created_at LIMIT ? OFFSET ?",
        (team_id, params.limit + 1, params.offset),
    )
    part, nxt = params.from_overfetch(rows)
    return api.Page[api.TeamToken](items=[_token_out(ctx, r["id"]) for r in part], next_cursor=nxt)


@router.delete("/teams/{team_id}/tokens/{token_id}", status_code=204)
def delete_token(team_id: str, token_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    require_team(principal_of(request), team_id)
    cur = ctx.db.execute("DELETE FROM team_tokens WHERE id = ? AND team_id = ?", (token_id, team_id))
    if cur.rowcount == 0:
        raise NotFound(f"Team token {token_id} was not found.")
    return Response(status_code=204)
