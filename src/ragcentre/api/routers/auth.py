"""Authentication introspection: who the current token belongs to."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ragcentre.api.authz import principal_of
from ragcentre.api.deps import get_ctx
from ragcentre.context import AppContext
from ragcentre.models import api

router = APIRouter(tags=["auth"])


@router.get("/auth/whoami", response_model=api.WhoAmI)
def whoami(request: Request, ctx: AppContext = Depends(get_ctx)) -> api.WhoAmI:
    p = principal_of(request)
    team_name = None
    if p.team_id is not None:
        team_name = ctx.db.scalar("SELECT name FROM teams WHERE id = ?", (p.team_id,))
    return api.WhoAmI(scope=p.scope, team_id=p.team_id, team_name=team_name)
