"""Sync sources: connectors attached to a corpus, plus their per-remote-item state."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from ragcentre.api.authz import principal_of, require_corpus, require_provider, require_source, team_of_corpus
from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.api.permissions import SessionRoute
from ragcentre.connectors.scopes import validate_scope
from ragcentre.connectors.sync import enqueue_sync
from ragcentre.context import AppContext
from ragcentre.errors import Conflict, NotFound, ValidationFailed
from ragcentre.hashing import new_id
from ragcentre.models import api
from ragcentre.timeutil import after, now_iso

router = APIRouter(route_class=SessionRoute, tags=["sources"])


def _source_out(ctx: AppContext, source_id: str) -> api.Source:
    row = ctx.db.one("SELECT * FROM sync_sources WHERE id = ?", (source_id,))
    if row is None:
        raise NotFound(f"Source {source_id} was not found.")
    return api.Source(
        id=row["id"],
        corpus_id=row["corpus_id"],
        provider_id=row["provider_id"],
        name=row["name"],
        scope=json.loads(row["scope"]),
        schedule_seconds=int(row["schedule_seconds"]),
        cursor=row["cursor"],
        status=row["status"],
        next_run_at=row["next_run_at"],
        last_run_at=row["last_run_at"],
        last_error=row["last_error"],
        metadata_template=json.loads(row["metadata_template"]),
        attributes_template=json.loads(row["attributes_template"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _item_out(row: Any) -> api.SourceItem:
    return api.SourceItem(
        source_id=row["source_id"],
        remote_id=row["remote_id"],
        remote_version=row["remote_version"],
        document_id=row["document_id"],
        remote_path=row["remote_path"],
        remote_mime=row["remote_mime"],
        remote_size=row["remote_size"],
        remote_modified=row["remote_modified"],
        skipped_reason=row["skipped_reason"],
        last_seen_at=row["last_seen_at"],
    )


@router.get("/corpora/{corpus_id}/sources", response_model=api.Page[api.Source])
def list_sources(
    corpus_id: str,
    request: Request,
    params: PageParams = Depends(page_params),
    ctx: AppContext = Depends(get_ctx),
) -> api.Page[api.Source]:
    require_corpus(ctx, principal_of(request), corpus_id)
    rows = ctx.db.all(
        "SELECT id FROM sync_sources WHERE corpus_id = ? ORDER BY created_at LIMIT ? OFFSET ?",
        (corpus_id, params.limit + 1, params.offset),
    )
    part, nxt = params.from_overfetch(rows)
    return api.Page[api.Source](items=[_source_out(ctx, r["id"]) for r in part], next_cursor=nxt)


@router.post("/corpora/{corpus_id}/sources", response_model=api.Source, status_code=201)
def create_source(
    corpus_id: str, body: api.SourceCreate, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.Source:
    principal = principal_of(request)
    require_corpus(ctx, principal, corpus_id)
    require_provider(ctx, principal, body.provider_id)
    provider = ctx.providers.get(body.provider_id)
    if provider.team_id != team_of_corpus(ctx, corpus_id):
        raise ValidationFailed("The connector provider must belong to the corpus's team.")
    if provider.kind != "connector":
        raise ValidationFailed(f"Provider {provider.name} is a {provider.kind}, not a connector.")
    name = body.name.strip()
    if not name:
        raise ValidationFailed("Give the source a name.")
    validate_scope(provider.type, body.scope)
    source_id = new_id()
    now = now_iso()
    next_run = now if body.schedule_seconds > 0 else None
    try:
        ctx.db.execute(
            "INSERT INTO sync_sources (id, corpus_id, provider_id, name, scope, schedule_seconds, status, "
            "next_run_at, metadata_template, attributes_template, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'idle', ?, ?, ?, ?, ?)",
            (
                source_id,
                corpus_id,
                body.provider_id,
                name,
                json.dumps(body.scope),
                body.schedule_seconds,
                next_run,
                json.dumps(body.metadata_template),
                json.dumps(body.attributes_template),
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"A source named {name!r} already exists in this corpus.") from None
    return _source_out(ctx, source_id)


@router.get("/sources/{source_id}", response_model=api.Source)
def get_source(source_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.Source:
    require_source(ctx, principal_of(request), source_id)
    return _source_out(ctx, source_id)


@router.patch("/sources/{source_id}", response_model=api.Source)
def patch_source(
    source_id: str, body: api.SourcePatch, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.Source:
    require_source(ctx, principal_of(request), source_id)
    row = ctx.db.one("SELECT * FROM sync_sources WHERE id = ?", (source_id,))
    if row is None:
        raise NotFound(f"Source {source_id} was not found.")
    sets: list[str] = []
    params: list[Any] = []
    if body.scope is not None:
        validate_scope(ctx.providers.get(row["provider_id"]).type, body.scope)
        sets.append("scope = ?")
        params.append(json.dumps(body.scope))
    if body.metadata_template is not None:
        sets.append("metadata_template = ?")
        params.append(json.dumps(body.metadata_template))
    if body.attributes_template is not None:
        sets.append("attributes_template = ?")
        params.append(json.dumps(body.attributes_template))
    if body.schedule_seconds is not None:
        sets.append("schedule_seconds = ?")
        params.append(body.schedule_seconds)
        if row["status"] == "idle":
            sets.append("next_run_at = ?")
            params.append(after(body.schedule_seconds) if body.schedule_seconds else None)
    if sets:
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(source_id)
        ctx.db.execute(f"UPDATE sync_sources SET {', '.join(sets)} WHERE id = ?", params)
    return _source_out(ctx, source_id)


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    require_source(ctx, principal_of(request), source_id)
    if ctx.db.execute("DELETE FROM sync_sources WHERE id = ?", (source_id,)).rowcount == 0:
        raise NotFound(f"Source {source_id} was not found.")
    return Response(status_code=204)


@router.post("/sources/{source_id}/sync", response_model=api.Source)
def sync_source(source_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.Source:
    require_source(ctx, principal_of(request), source_id)
    enqueue_sync(ctx, source_id)
    return _source_out(ctx, source_id)


@router.get("/sources/{source_id}/items", response_model=api.Page[api.SourceItem])
def list_items(
    source_id: str,
    request: Request,
    params: PageParams = Depends(page_params),
    ctx: AppContext = Depends(get_ctx),
) -> api.Page[api.SourceItem]:
    require_source(ctx, principal_of(request), source_id)
    rows = ctx.db.all(
        "SELECT * FROM source_items WHERE source_id = ? ORDER BY remote_id LIMIT ? OFFSET ?",
        (source_id, params.limit + 1, params.offset),
    )
    part, nxt = params.from_overfetch(rows)
    return api.Page[api.SourceItem](items=[_item_out(r) for r in part], next_cursor=nxt)
