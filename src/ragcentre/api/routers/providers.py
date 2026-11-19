"""Provider types, providers, secrets and the provider test action (sections 8, 15, 16)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response

from ragcentre.api.deps import get_ctx
from ragcentre.api.pagination import PageParams, page_params
from ragcentre.context import AppContext
from ragcentre.models import api
from ragcentre.providers.manager import ProviderRow
from ragcentre.providers.registry import get_type, provider_types

router = APIRouter(tags=["providers"])


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
    params: PageParams = Depends(page_params), ctx: AppContext = Depends(get_ctx)
) -> api.Page[api.Provider]:
    usage = ctx.providers.usage_counts()
    part, nxt = params.page(ctx.providers.list())
    return api.Page[api.Provider](items=[provider_out(ctx, r, usage) for r in part], next_cursor=nxt)


@router.post("/providers", response_model=api.Provider, status_code=201)
def create_provider(body: api.ProviderCreate, ctx: AppContext = Depends(get_ctx)) -> api.Provider:
    row = ctx.providers.create(body.name, body.kind, body.type, body.config)
    return provider_out(ctx, row)


@router.get("/providers/{provider_id}", response_model=api.Provider)
def get_provider(provider_id: str, ctx: AppContext = Depends(get_ctx)) -> api.Provider:
    return provider_out(ctx, ctx.providers.get(provider_id))


@router.patch("/providers/{provider_id}", response_model=api.Provider)
def patch_provider(provider_id: str, body: api.ProviderPatch, ctx: AppContext = Depends(get_ctx)) -> api.Provider:
    return provider_out(ctx, ctx.providers.update(provider_id, body.config))


@router.delete("/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: str, ctx: AppContext = Depends(get_ctx)) -> Response:
    ctx.providers.delete(provider_id)
    return Response(status_code=204)


@router.post("/providers/{provider_id}/test", response_model=api.ProviderTestResult)
def test_provider(provider_id: str, deep: bool = False, ctx: AppContext = Depends(get_ctx)) -> api.ProviderTestResult:
    """Check that a provider works. With ``deep=true`` a parser service also parses a sample
    document and reports which RAGCentre features its output feeds."""
    return api.ProviderTestResult.model_validate(ctx.providers.test(provider_id, deep=deep))


@router.put("/providers/{provider_id}/secrets/{key}", response_model=api.Provider)
def set_secret(provider_id: str, key: str, body: api.SecretSet, ctx: AppContext = Depends(get_ctx)) -> api.Provider:
    ctx.providers.set_secret(provider_id, key, env=body.env, value=body.value)
    return provider_out(ctx, ctx.providers.get(provider_id))


@router.delete("/providers/{provider_id}/secrets/{key}", response_model=api.Provider)
def delete_secret(provider_id: str, key: str, ctx: AppContext = Depends(get_ctx)) -> api.Provider:
    ctx.providers.delete_secret(provider_id, key)
    return provider_out(ctx, ctx.providers.get(provider_id))
