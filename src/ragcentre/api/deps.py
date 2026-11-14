"""FastAPI dependencies: the application context and the bearer-token check."""

from __future__ import annotations

import hmac

from fastapi import Request

from ragcentre.context import AppContext
from ragcentre.errors import Unauthorized

PUBLIC_PATHS = frozenset({"/v1/health"})


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def require_token(request: Request) -> None:
    """When ``RAGCENTRE_API_TOKEN`` is set, every request but ``/v1/health`` needs the token."""
    ctx = get_ctx(request)
    token = ctx.settings.api_token
    if not token or request.url.path in PUBLIC_PATHS:
        return
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), token.encode()):
        raise Unauthorized("This server requires an access token. Send it as 'Authorization: Bearer <token>'.")
