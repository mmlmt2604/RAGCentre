"""Explicit session route policy. Bearer permissions remain in RequestGuard and ownership helpers."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, Literal

from fastapi import Request, Response
from fastapi.routing import APIRoute

from ragcentre.api.authz import (
    principal_of,
    require_corpus,
    require_dataset,
    require_document,
    require_eval_run,
    require_node_run,
    require_pipeline,
    require_provider,
    require_source,
    require_team,
)
from ragcentre.errors import ErrorCode, Forbidden, RagError

Policy = Literal["public", "account", "admin", "viewer", "editor", "owner"]
POLICIES: dict[tuple[str, str], Policy] = {}


def _register(policy: Policy, method: str, paths: str) -> None:
    for path in paths.split():
        POLICIES[(method, "/v1" + path)] = policy


_register("public", "GET", "/health /auth/config")
_register("public", "POST", "/auth/login /auth/setup")
_register("account", "GET", "/auth/me /auth/csrf /auth/whoami /me/teams")
_register("account", "POST", "/auth/logout /auth/password")
_register("admin", "GET", "/users /teams /ocr-services")
_register(
    "admin",
    "POST",
    "/users /users/{user_id}/setup-token /teams /ocr-services/scan /providers/{provider_id}/ocr-check /admin/gc /admin/cache/clear",
)
_register("admin", "PATCH", "/users/{user_id}")
_register(
    "viewer",
    "GET",
    """
/corpora /corpora/{corpus_id} /corpora/{corpus_id}/documents /corpora/{corpus_id}/pipelines
/documents/{document_id} /documents/{document_id}/content /documents/{document_id}/parsed /documents/{document_id}/chunks
/pipelines/{pipeline_id} /pipelines/{pipeline_id}/status /pipelines/{pipeline_id}/diff
/corpora/{corpus_id}/eval/datasets /corpora/{corpus_id}/eval/runs
/eval/datasets/{dataset_id} /eval/datasets/{dataset_id}/items /eval/runs/{run_id} /eval/runs/{run_id}/items
/node-runs /teams/{team_id} /provider-summaries
""",
)
_register("viewer", "POST", "/corpora/{corpus_id}/query")
_register("editor", "GET", "/providers /providers/{provider_id} /provider-types")
_register(
    "editor",
    "POST",
    "/corpora /corpora/{corpus_id}/documents /corpora/{corpus_id}/pipelines /pipelines/{pipeline_id}/archive /pipelines/{pipeline_id}/retry-failed /node-runs/{run_id}/retry /corpora/{corpus_id}/eval/datasets /eval/runs",
)
_register("editor", "PATCH", "/corpora/{corpus_id} /corpora/{corpus_id}/metadata-schema /documents/{document_id}")
_register("editor", "PUT", "/corpora/{corpus_id}/active-pipeline /documents/{document_id}/content")
_register("editor", "DELETE", "/documents/{document_id} /eval/datasets/{dataset_id}")
_register(
    "owner",
    "GET",
    "/corpora/{corpus_id}/sources /sources/{source_id} /sources/{source_id}/items /teams/{team_id}/members /teams/{team_id}/tokens",
)
_register(
    "owner",
    "POST",
    "/providers /providers/{provider_id}/test /corpora/{corpus_id}/sources /sources/{source_id}/sync /teams/{team_id}/tokens",
)
_register("owner", "PATCH", "/providers/{provider_id} /sources/{source_id}")
_register("owner", "PUT", "/providers/{provider_id}/secrets/{key} /teams/{team_id}/members/{username}")
_register(
    "owner",
    "DELETE",
    "/providers/{provider_id} /providers/{provider_id}/secrets/{key} /sources/{source_id} /teams/{team_id}/members/{username} /teams/{team_id}/tokens/{token_id}",
)


def authorize(request: Request, route: APIRoute) -> None:
    p = principal_of(request)
    if p.actor_kind != "user":
        return
    path = route.path if route.path.startswith("/v1/") else "/v1" + route.path
    policy = POLICIES.get((request.method, path))
    if policy is None:
        raise Forbidden("This route is not available to user sessions.")
    if policy in {"public", "account"}:
        return
    if policy == "admin":
        if not p.is_admin:
            raise Forbidden("This action requires a platform administrator.")
        return
    if p.team_id is None:
        raise RagError("Select a team before accessing its resources.", code=ErrorCode.TEAM_REQUIRED, status=400)
    ctx = request.app.state.ctx
    checks = {
        "corpus_id": require_corpus,
        "document_id": require_document,
        "pipeline_id": require_pipeline,
        "dataset_id": require_dataset,
        "provider_id": require_provider,
        "source_id": require_source,
    }
    for name, check in checks.items():
        if name in request.path_params:
            check(ctx, p, str(request.path_params[name]))
    if "team_id" in request.path_params:
        require_team(p, str(request.path_params["team_id"]))
    if "run_id" in request.path_params:
        if path.startswith("/v1/node-runs/"):
            require_node_run(ctx, p, int(request.path_params["run_id"]))
        else:
            require_eval_run(ctx, p, str(request.path_params["run_id"]))
    ranks = {"viewer": 1, "editor": 2, "owner": 3}
    if not p.is_admin and ranks.get(p.role or "", 0) < ranks[policy]:
        raise Forbidden("Your team role does not allow this action.")


class SessionRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()
        path = self.path if self.path.startswith("/v1/") else "/v1" + self.path

        async def handler(request: Request) -> Response:
            if POLICIES.get((request.method, path)) != "public":
                authorize(request, self)
            return await original(request)

        return handler
