"""Confluence Cloud connector: list pages in a space and convert storage format to Markdown.

Phase 1 lists the whole space (``full=True``) and diffs on ``version.number``; there is no global
change feed to follow. Scope is ``{"space_key": "TEAM"}``. Auth is basic (email + API token).
"""

from __future__ import annotations

from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import Field

from ragcentre.connectors._http import basic_auth, request, request_json
from ragcentre.connectors.base import FetchedFile, RemoteItem, ScanResult
from ragcentre.errors import ExecutionError
from ragcentre.providers.base import BuildEnv, ProviderConfig, ProviderTypeSpec

PAGE_SIZE = 50


class _StorageToMarkdown(HTMLParser):
    """Best-effort converter for Confluence Cloud's storage format (XHTML-ish, with ``ac:`` macros).

    Handles the common cases — headings, paragraphs, lists, code blocks, tables, emphasis and links —
    and drops macros and unknown tags rather than failing on them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0
        self._list_stack: list[str] = []
        self._href: str | None = None

    def _local(self, tag: str) -> str:
        return tag.split(":", 1)[-1].lower()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        local = self._local(tag)
        attr_dict = dict(attrs)
        if self._skip:
            if local == "structured-macro":
                self._skip += 1
            return
        if local in ("structured-macro", "link-macro", "unmigrated-inline-wiki-markup"):
            self._skip = 1
            return
        if local in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(local[1]) + " ")
        elif local == "p":
            self.out.append("\n\n")
        elif local == "br":
            self.out.append("\n")
        elif local in ("strong", "b"):
            self.out.append("**")
        elif local in ("em", "i"):
            self.out.append("_")
        elif local == "code":
            self.out.append("`")
        elif local == "li":
            marker = "- " if (self._list_stack and self._list_stack[-1] == "ul") else "1. "
            self.out.append("\n" + "  " * max(0, len(self._list_stack) - 1) + marker)
        elif local in ("ul", "ol"):
            self._list_stack.append(local)
        elif local == "blockquote":
            self.out.append("\n\n> ")
        elif local == "a":
            self._href = attr_dict.get("href") or ""
            self.out.append("[")

    def handle_endtag(self, tag: str) -> None:
        local = self._local(tag)
        if self._skip:
            if local == "structured-macro":
                self._skip -= 1
            return
        if local in ("h1", "h2", "h3", "h4", "h5", "h6", "p"):
            self.out.append("\n")
        elif local in ("strong", "b"):
            self.out.append("**")
        elif local in ("em", "i"):
            self.out.append("_")
        elif local == "code":
            self.out.append("`")
        elif local in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
        elif local == "blockquote":
            self.out.append("\n\n")
        elif local == "a":
            if self._href:
                self.out.append(f"]({self._href})")
            self._href = None

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._local(tag) == "br":
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.out.append(data)


def _storage_to_markdown(html: str) -> str:
    parser = _StorageToMarkdown()
    parser.feed(html)
    text = "".join(parser.out)
    # Collapse the excess blank lines the parser's newline padding can produce.
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blank = 0
    for line in lines:
        if not line:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    return "\n".join(out).strip() + "\n"


class ConfluenceConfig(ProviderConfig):
    base_url: str = Field(description="Confluence Cloud URL, e.g. https://your-site.atlassian.net")
    timeout_seconds: float = Field(default=120, gt=0, le=3600, description="Request timeout in seconds")
    max_concurrency: int = Field(default=4, ge=1, le=64, description="Parallel requests")


class ConfluenceConnector:
    def __init__(self, config: ConfluenceConfig, secrets: dict[str, str], env: BuildEnv) -> None:
        self.config = config
        self.name = env.provider_name
        self.base_url = config.base_url.rstrip("/")
        email = secrets.get("email", "")
        api_token = secrets.get("api_token", "")
        self.client = env.http(
            timeout=httpx.Timeout(config.timeout_seconds),
            headers={"Authorization": basic_auth(email, api_token)},
        )
        self._space_ids: dict[str, str] = {}

    def _space_id(self, space_key: str) -> str:
        cached = self._space_ids.get(space_key)
        if cached:
            return cached
        body = request_json(
            self.client, "GET", f"{self.base_url}/wiki/api/v2/spaces", name=self.name, params={"keys": space_key}
        )
        results = body.get("results") if isinstance(body, dict) else None
        if not results:
            raise ExecutionError(
                "invalid_scope", f"Confluence space {space_key!r} was not found.", retryable=False, provider=self.name
            )
        space_id = results[0].get("id")
        if not isinstance(space_id, str) or not space_id:
            raise ExecutionError(
                "invalid_scope", f"Confluence space {space_key!r} was not found.", retryable=False, provider=self.name
            )
        self._space_ids[space_key] = space_id
        return space_id

    def check(self) -> None:
        request(self.client, "GET", f"{self.base_url}/wiki/api/v2/spaces", name=self.name, params={"limit": 1})

    def scan(self, scope: Mapping[str, Any], cursor: str | None) -> ScanResult:
        space_key = scope.get("space_key")
        if not isinstance(space_key, str) or not space_key:
            raise ExecutionError(
                "invalid_scope", "Confluence scope requires a string 'space_key'.", retryable=False, provider=self.name
            )
        space_id = self._space_id(space_key)
        params: dict[str, Any] = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        body = request_json(
            self.client, "GET", f"{self.base_url}/wiki/api/v2/spaces/{space_id}/pages",
            name=self.name, params=params,
        )
        items: list[RemoteItem] = []
        for page in body.get("results", []) if isinstance(body, dict) else []:
            if not isinstance(page, dict) or page.get("status") != "current":
                continue
            page_id = page.get("id")
            if not isinstance(page_id, str):
                continue
            version = page.get("version") or {}
            items.append(
                RemoteItem(
                    remote_id=page_id,
                    name=page.get("title") or page_id,
                    mime_type="text/markdown",
                    size=None,
                    version=str(version.get("number", "")),
                    modified_at=version.get("createdAt"),
                    deleted=False,
                    path=page.get("parentId"),
                )
            )
        next_cursor = None
        links = body.get("_links") if isinstance(body, dict) else None
        if isinstance(links, dict) and links.get("next"):
            next_cursor = parse_qs(urlparse(links["next"]).query).get("cursor", [None])[0]
        return ScanResult(items=iter(items), next_cursor=next_cursor, full=True)

    def fetch(self, scope: Mapping[str, Any], item: RemoteItem) -> FetchedFile:
        space_key = scope.get("space_key")
        body = request_json(
            self.client, "GET", f"{self.base_url}/wiki/api/v2/pages/{item.remote_id}",
            name=self.name, params={"body-format": "storage"},
        )
        storage = ((body.get("body") or {}).get("storage") or {}).get("value") if isinstance(body, dict) else None
        markdown = _storage_to_markdown(storage) if isinstance(storage, str) else ""
        attributes = {
            "space_key": space_key,
            "page_id": item.remote_id,
            "source_url": f"{self.base_url}/wiki/spaces/{space_key}/pages/{item.remote_id}",
        }
        return FetchedFile(
            content=markdown.encode(),
            filename=f"{item.name}.md",
            mime_type="text/markdown",
            version=item.version,
            metadata={},
            attributes=attributes,
        )

    def close(self) -> None:
        self.client.close()


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> ConfluenceConnector:
    assert isinstance(config, ConfluenceConfig)
    return ConfluenceConnector(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="connector",
    type="confluence",
    label="Confluence Cloud",
    description="Sync pages from a Confluence Cloud space, converted to Markdown.",
    config_model=ConfluenceConfig,
    fingerprint_fields=("base_url",),
    secret_keys=("email", "api_token"),
    factory=_factory,
)
