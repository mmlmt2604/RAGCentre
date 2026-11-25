"""Shared plumbing for REST connectors: classified requests, JSON helpers, token caching."""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from typing import Any

import httpx

from ragcentre.errors import ExecutionError
from ragcentre.providers.base import classify_exception, classify_response, invalid_response


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    name: str,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    try:
        resp = client.request(method, url, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise classify_exception(exc, name, url) from exc
    error = classify_response(resp, name)
    if error is not None:
        raise error
    return resp


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    name: str,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> Any:
    resp = request(client, method, url, name=name, headers=headers, **kwargs)
    try:
        return resp.json()
    except ValueError as exc:
        raise invalid_response(name, f"{url} did not return JSON") from exc


def basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


class TokenCache:
    """Cached OAuth access token, refreshed shortly before it expires."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self, fetch: Callable[[], tuple[str, float]]) -> str:
        if self._token is not None and time.monotonic() < self._expires_at:
            return self._token
        token, expires_in = fetch()
        if not token:
            raise ExecutionError("provider_error", "upstream returned no access token", retryable=False)
        self._token = token
        self._expires_at = time.monotonic() + max(0.0, float(expires_in) - 60.0)
        return token
