"""A small async client for the read side of RAGCentre's HTTP API.

It can only send GET requests, plus the one POST that searches a corpus. That is what makes the
MCP server read-only even if it is given a token that could do more.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from ragcentre_mcp import __version__
from ragcentre_mcp.models import ChunkWindow, Corpus, Health, Outline, QueryResponse, WhoAmI

_QUERY_PATH = re.compile(r"^/corpora/[^/]+/query$")


class ApiError(Exception):
    """RAGCentre answered with an error body ``{"error": {"code", "message", "details"}}``."""

    def __init__(self, status: int, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = dict(details or {})


class Unreachable(Exception):
    """No RAGCentre server answered."""


def redact_url(url: str) -> str:
    """The URL without user information, safe for messages."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _seg(value: str) -> str:
    return quote(value, safe="")


class RagcentreApi:
    def __init__(
        self,
        url: str,
        token: str | None,
        *,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        headers = {"User-Agent": f"ragcentre-mcp/{__version__}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=f"{self.url}/v1", headers=headers, timeout=timeout, transport=transport, follow_redirects=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not (method == "GET" or (method == "POST" and _QUERY_PATH.match(path))):
            raise RuntimeError(f"ragcentre-mcp is read-only and never sends {method} {path}")
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.TransportError:
            raise Unreachable(f"RAGCentre is not reachable at {redact_url(self.url)}.") from None
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error") or {}
            except (ValueError, AttributeError):
                err = {}
            details = err.get("details")
            raise ApiError(
                resp.status_code,
                str(err.get("code") or f"http_{resp.status_code}"),
                str(err.get("message") or resp.reason_phrase),
                details if isinstance(details, dict) else None,
            )
        return resp.json()

    async def health(self) -> Health:
        return Health.model_validate(await self._request("GET", "/health"))

    async def whoami(self) -> WhoAmI:
        return WhoAmI.model_validate(await self._request("GET", "/auth/whoami"))

    async def corpora(self) -> list[Corpus]:
        out: list[Corpus] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 500}
            if cursor:
                params["cursor"] = cursor
            page = await self._request("GET", "/corpora", params=params)
            out += [Corpus.model_validate(c) for c in page["items"]]
            cursor = page.get("next_cursor")
            if not cursor:
                return out

    async def query(
        self, corpus_id: str, query: str, *, top_k: int, filter: dict[str, Any] | None, max_chars: int
    ) -> QueryResponse:
        body: dict[str, Any] = {"query": query, "top_k": top_k, "max_chars": max_chars}
        if filter:
            body["filter"] = filter
        return QueryResponse.model_validate(await self._request("POST", f"/corpora/{_seg(corpus_id)}/query", json=body))

    async def chunk(self, chunk_id: str, *, before: int, after: int) -> ChunkWindow:
        data = await self._request("GET", f"/chunks/{_seg(chunk_id)}", params={"before": before, "after": after})
        return ChunkWindow.model_validate(data)

    async def outline(self, document_id: str) -> Outline:
        return Outline.model_validate(await self._request("GET", f"/documents/{_seg(document_id)}/outline"))
