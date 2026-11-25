"""Notion connector: sync pages the integration can see, converted to Markdown.

Full-list connector (``full=True``): every sync re-lists and diffs on ``last_edited_time``. Scope is
``{}`` (all pages shared with the integration), ``{"parent_page_id": "..."}`` (a page subtree) or
``{"database_id": "..."}`` (all pages in a database). Auth is an integration bearer token.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import httpx
from pydantic import Field

from ragcentre.connectors._http import request, request_json
from ragcentre.connectors.base import FetchedFile, RemoteItem, ScanResult
from ragcentre.providers.base import BuildEnv, ProviderConfig, ProviderTypeSpec

API_ROOT = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PAGE_SIZE = 100


class NotionConfig(ProviderConfig):
    timeout_seconds: float = Field(default=120, gt=0, le=3600, description="Request timeout in seconds")
    max_concurrency: int = Field(default=4, ge=1, le=64, description="Parallel requests")


def _plain_text(rich_text: Any) -> str:
    if not isinstance(rich_text, list):
        return ""
    return "".join(rt.get("plain_text", "") for rt in rich_text if isinstance(rt, dict))


def _page_title(page: dict[str, Any]) -> str:
    props = page.get("properties") or {}
    for prop in props.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            title = _plain_text(prop.get("title"))
            if title:
                return title
    return "Untitled"


def _blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type", "")
        data = block.get(kind) or {}
        text = _plain_text(data.get("rich_text"))
        if kind.startswith("heading_"):
            level = int(kind.split("_", 1)[1])
            out.append(f"\n{'#' * level} {text}\n")
        elif kind == "paragraph":
            out.append(f"{text}\n")
        elif kind in ("bulleted_list_item", "to_do"):
            out.append(f"- {text}\n")
        elif kind == "numbered_list_item":
            out.append(f"1. {text}\n")
        elif kind == "quote":
            out.append(f"> {text}\n")
        elif kind == "code":
            out.append(f"```\n{text}\n```\n")
        elif kind == "divider":
            out.append("\n---\n")
        elif kind == "callout":
            out.append(f"> {text}\n")
        elif kind == "image":
            url = (data.get("file") or {}).get("url") or (data.get("external") or {}).get("url") or ""
            if url:
                out.append(f"![{text or 'image'}]({url})\n")
        elif kind in ("child_page", "child_database"):
            out.append(f"- {text}\n")
    return "".join(out)


class NotionConnector:
    def __init__(self, config: NotionConfig, secrets: dict[str, str], env: BuildEnv) -> None:
        self.config = config
        self.name = env.provider_name
        token = secrets.get("api_token", "")
        self.client = env.http(
            timeout=httpx.Timeout(config.timeout_seconds),
            headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
        )

    def check(self) -> None:
        request(self.client, "GET", f"{API_ROOT}/users/me", name=self.name)

    def _list_page(self, cursor: str | None, body: dict[str, Any]) -> dict[str, Any]:
        if cursor:
            body = {**body, "start_cursor": cursor}
        return cast(dict[str, Any], request_json(self.client, "POST", f"{API_ROOT}/search", name=self.name, json=body))

    def _list_database(self, cursor: str | None, database_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {"page_size": PAGE_SIZE}
        if cursor:
            body["start_cursor"] = cursor
        return cast(
            dict[str, Any],
            request_json(self.client, "POST", f"{API_ROOT}/databases/{database_id}/query", name=self.name, json=body),
        )

    def scan(self, scope: Mapping[str, Any], cursor: str | None) -> ScanResult:
        database_id = scope.get("database_id")
        parent_page_id = scope.get("parent_page_id")
        if database_id:
            body = self._list_database(cursor, str(database_id))
        else:
            search = {"page_size": PAGE_SIZE, "filter": {"value": "page", "property": "object"}}
            body = self._list_page(cursor, search)

        items: list[RemoteItem] = []
        for page in body.get("results", []) if isinstance(body, dict) else []:
            if not isinstance(page, dict):
                continue
            page_id = page.get("id")
            if not isinstance(page_id, str):
                continue
            parent = page.get("parent") or {}
            if parent_page_id and parent.get("page_id") != parent_page_id:
                continue
            items.append(
                RemoteItem(
                    remote_id=page_id,
                    name=_page_title(page),
                    mime_type="text/markdown",
                    size=None,
                    version=page.get("last_edited_time"),
                    modified_at=page.get("last_edited_time"),
                    deleted=False,
                    path=parent.get("page_id") or parent.get("database_id"),
                )
            )
        has_more = bool(body.get("has_more")) if isinstance(body, dict) else False
        next_cursor = body.get("next_cursor") if isinstance(body, dict) and has_more else None
        return ScanResult(items=iter(items), next_cursor=next_cursor, full=True)

    def _all_blocks(self, page_id: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": PAGE_SIZE}
            if cursor:
                params["start_cursor"] = cursor
            body = request_json(
                self.client, "GET", f"{API_ROOT}/blocks/{page_id}/children", name=self.name, params=params
            )
            results = body.get("results", []) if isinstance(body, dict) else []
            blocks.extend(r for r in results if isinstance(r, dict))
            if not (body.get("has_more") if isinstance(body, dict) else False):
                break
            cursor = body.get("next_cursor")
        return blocks

    def fetch(self, scope: Mapping[str, Any], item: RemoteItem) -> FetchedFile:
        page = request_json(self.client, "GET", f"{API_ROOT}/pages/{item.remote_id}", name=self.name)
        markdown = _blocks_to_markdown(self._all_blocks(item.remote_id))
        attributes = {
            "page_id": item.remote_id,
            "source_url": page.get("url", "") if isinstance(page, dict) else "",
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


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> NotionConnector:
    assert isinstance(config, NotionConfig)
    return NotionConnector(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="connector",
    type="notion",
    label="Notion",
    description="Sync pages an integration can see (or a database), converted to Markdown.",
    config_model=NotionConfig,
    fingerprint_fields=(),
    secret_keys=("api_token",),
    factory=_factory,
)
