"""Named API keys for MCP, CLI and other non-browser clients. Session-only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from ragcentre.api.deps import get_ctx, require_session
from ragcentre.context import AppContext
from ragcentre.errors import NotFound
from ragcentre.models import api

router = APIRouter(tags=["keys"], dependencies=[Depends(require_session)])


@router.get("/keys", response_model=api.Page[api.ApiKey])
def list_keys(ctx: AppContext = Depends(get_ctx)) -> api.Page[api.ApiKey]:
    items = [api.ApiKey(**k) for k in ctx.accounts.list_keys()]
    return api.Page[api.ApiKey](items=items, next_cursor=None)


@router.post("/keys", response_model=api.ApiKeyCreated, status_code=201)
def create_key(body: api.ApiKeyCreate, ctx: AppContext = Depends(get_ctx)) -> api.ApiKeyCreated:
    created = ctx.accounts.create_key(body.name, body.scope)
    return api.ApiKeyCreated(
        id=created["id"],
        name=created["name"],
        scope=created["scope"],
        key=created["key"],
        created_at=created["created_at"],
    )


@router.delete("/keys/{key_id}", status_code=204)
def delete_key(key_id: str, ctx: AppContext = Depends(get_ctx)) -> Response:
    if not ctx.accounts.revoke_key(key_id):
        raise NotFound(f"Key {key_id} was not found.")
    return Response(status_code=204)
