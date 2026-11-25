"""Provider types, providers, secrets and the provider test action (sections 8, 15, 16)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from ragcentre.api.authz import DEFAULT_TEAM_ID, principal_of, require_provider, require_provider_owner
from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.api.permissions import SessionRoute
from ragcentre.context import AppContext
from ragcentre.errors import Forbidden, NotFound
from ragcentre.models import api
from ragcentre.providers.manager import ProviderRow
from ragcentre.providers.registry import get_type, provider_types

router = APIRouter(route_class=SessionRoute, tags=["providers"])


def _schema(spec: Any) -> dict[str, Any]:
    schema: dict[str, Any] = spec.config_model.model_json_schema()
    schema.pop("title", None)
    return schema


def provider_out(ctx: AppContext, row: ProviderRow, usage: dict[str, int] | None = None) -> api.Provider:
    spec = get_type(row.kind, row.type)
    counts = usage if usage is not None else ctx.providers.usage_counts()
    return api.Provider(
        id=row.id,
        name=row.name,
        kind=row.kind,  # type: ignore[arg-type]
        type=row.type,
        config=row.config,
        fingerprint=row.fingerprint,
        secrets={k: api.SecretState(**v) for k, v in ctx.providers.secret_states(row.id, spec).items()},
        builtin=row.builtin,
        used_by_pipelines=counts.get(row.id, 0),
        team_id=row.team_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/provider-types", response_model=api.Page[api.ProviderType])
def list_provider_types(params: PageParams = Depends(page_params)) -> api.Page[api.ProviderType]:
    items = []
    for spec in sorted(provider_types().values(), key=lambda s: (s.kind, s.type)):
        caps = None
        if spec.capabilities is not None:
            caps = api.StoreCapabilities(
                metrics=sorted(spec.capabilities.metrics),
                max_batch=spec.capabilities.max_batch,
                multi_vector=spec.capabilities.multi_vector,
                native_hybrid=spec.capabilities.native_hybrid,
                max_dim=spec.capabilities.max_dim,
                consistency=spec.capabilities.consistency,
                filter_exact=spec.capabilities.filter_exact,
                max_collections=spec.capabilities.max_collections,
            )
        items.append(
            api.ProviderType(
                kind=spec.kind,
                type=spec.type,
                label=spec.label,
                description=spec.description,
                config_schema=_schema(spec),
                fingerprint_fields=list(spec.fingerprint_fields),
                secret_keys=list(spec.secret_keys),
                capabilities=caps,
            )
        )
    part, nxt = params.page(items)
    return api.Page[api.ProviderType](items=part, next_cursor=nxt)


@router.get("/providers", response_model=api.Page[api.Provider])
def list_providers(
    request: Request, params: PageParams = Depends(page_params), ctx: AppContext = Depends(get_ctx)
) -> api.Page[api.Provider]:
    principal = principal_of(request)
    usage = ctx.providers.usage_counts(None if principal.unrestricted else principal.team_id)
    rows = ctx.providers.list() if principal.unrestricted else ctx.providers.list_visible(principal.team_id or "")
    part, nxt = params.page(rows)
    return api.Page[api.Provider](items=[provider_out(ctx, r, usage) for r in part], next_cursor=nxt)


@router.get("/provider-summaries", response_model=api.Page[api.ProviderSummary])
def provider_summaries(
    request: Request, params: PageParams = Depends(page_params), ctx: AppContext = Depends(get_ctx)
) -> api.Page[api.ProviderSummary]:
    principal = principal_of(request)
    rows = ctx.providers.list() if principal.unrestricted else ctx.providers.list_visible(principal.team_id or "")
    part, nxt = params.page(rows)
    return api.Page[api.ProviderSummary](
        items=[
            api.ProviderSummary.model_validate(
                dict(id=r.id, name=r.name, kind=r.kind, type=r.type, builtin=r.builtin, team_id=r.team_id)
            )
            for r in part
        ],
        next_cursor=nxt,
    )


@router.post("/providers", response_model=api.Provider, status_code=201)
def create_provider(body: api.ProviderCreate, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.Provider:
    principal = principal_of(request)
    if principal.unrestricted:
        team_id = body.team_id or DEFAULT_TEAM_ID
        if ctx.db.one("SELECT id FROM teams WHERE id = ?", (team_id,)) is None:
            raise NotFound(f"Team {team_id} was not found.")
    else:
        assert principal.team_id is not None
        team_id = principal.team_id
        if body.team_id is not None and body.team_id != team_id:
            raise Forbidden("A team token can only create providers in its own team.")
    row = ctx.providers.create(body.name, body.kind, body.type, body.config, team_id=team_id)
    return provider_out(
        ctx,
        row,
        ctx.providers.usage_counts(None if principal_of(request).unrestricted else principal_of(request).team_id),
    )


@router.get("/providers/{provider_id}", response_model=api.Provider)
def get_provider(provider_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.Provider:
    require_provider(ctx, principal_of(request), provider_id)
    return provider_out(
        ctx,
        ctx.providers.get(provider_id),
        ctx.providers.usage_counts(None if principal_of(request).unrestricted else principal_of(request).team_id),
    )


@router.patch("/providers/{provider_id}", response_model=api.Provider)
def patch_provider(
    provider_id: str, body: api.ProviderPatch, request: Request, response: Response, ctx: AppContext = Depends(get_ctx)
) -> api.Provider:
    require_provider_owner(ctx, principal_of(request), provider_id)
    row = ctx.providers.update(provider_id, body.config)
    if row.secrets_reset:
        response.headers["X-RAGCentre-Secrets-Reset"] = "true"
    return provider_out(
        ctx,
        row,
        ctx.providers.usage_counts(None if principal_of(request).unrestricted else principal_of(request).team_id),
    )


@router.delete("/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    require_provider_owner(ctx, principal_of(request), provider_id)
    ctx.providers.delete(provider_id)
    return Response(status_code=204)


@router.post("/providers/{provider_id}/test", response_model=api.ProviderTestResult, response_model_exclude_unset=True)
def test_provider(
    provider_id: str, request: Request, deep: bool = False, ctx: AppContext = Depends(get_ctx)
) -> api.ProviderTestResult:
    """Check that a provider works. With ``deep=true`` a parser service also parses a sample
    document and reports which RAGCentre features its output feeds."""
    require_provider(ctx, principal_of(request), provider_id)
    return api.ProviderTestResult.model_validate(ctx.providers.test(provider_id, deep=deep))


@router.put("/providers/{provider_id}/secrets/{key}", response_model=api.Provider)
def set_secret(
    provider_id: str, key: str, body: api.SecretSet, request: Request, ctx: AppContext = Depends(get_ctx)
) -> api.Provider:
    require_provider_owner(ctx, principal_of(request), provider_id)
    ctx.providers.set_secret(provider_id, key, env=body.env, value=body.value)
    return provider_out(
        ctx,
        ctx.providers.get(provider_id),
        ctx.providers.usage_counts(None if principal_of(request).unrestricted else principal_of(request).team_id),
    )


@router.delete("/providers/{provider_id}/secrets/{key}", response_model=api.Provider)
def delete_secret(provider_id: str, key: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> api.Provider:
    require_provider_owner(ctx, principal_of(request), provider_id)
    ctx.providers.delete_secret(provider_id, key)
    return provider_out(
        ctx,
        ctx.providers.get(provider_id),
        ctx.providers.usage_counts(None if principal_of(request).unrestricted else principal_of(request).team_id),
    )
