"""FastAPI dependencies: the application context and the resolved principal."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request

from ragcentre.context import AppContext
from ragcentre.errors import Unauthorized


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def get_principal(request: Request) -> dict[str, Any]:
    """The principal the request guard resolved (``kind``, ``scope``, and credential-specific fields).

    When the guard is absent (``create_app(None)`` for OpenAPI generation) it reports an anonymous
    principal; no request is served in that configuration.
    """
    principal: dict[str, Any] = request.scope.get("ragcentre_principal", {"kind": "none", "scope": "none"})
    return principal


def require_session(principal: dict[str, Any] = Depends(get_principal)) -> dict[str, Any]:
    """Reject non-session callers. Account/keys endpoints are browser-session only, even for the
    legacy admin token and full keys, so a leaked bearer credential cannot mint accounts or keys."""
    if principal.get("kind") != "session":
        raise Unauthorized("Sign in to use this endpoint.")
    return principal
