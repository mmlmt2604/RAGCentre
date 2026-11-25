from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx

from ragcentre.connectors.notion import NotionConfig, NotionConnector, _blocks_to_markdown
from ragcentre.providers.base import BuildEnv


def _env(handler: Any) -> BuildEnv:
    transport = httpx.MockTransport(handler)

    def http(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=transport, **kwargs)

    return BuildEnv("p", "notion", http, Path("."), threading.BoundedSemaphore(1))


def _connector(handler: Any) -> NotionConnector:
    return NotionConnector(NotionConfig(), {"api_token": "tok"}, _env(handler))


def test_blocks_to_markdown() -> None:
    blocks = [
        {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Title"}]}},
        {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Body"}]}},
        {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "one"}]}},
    ]
    assert _blocks_to_markdown(blocks) == "\n# Title\nBody\n- one\n"


def test_scan_and_fetch() -> None:
    search = {
        "results": [
            {
                "id": "page1",
                "last_edited_time": "2026-01-01T00:00:00Z",
                "properties": {"title": {"type": "title", "title": [{"plain_text": "Welcome"}]}},
                "parent": {"type": "page_id", "page_id": "root"},
            }
        ],
        "has_more": False,
    }
    page = {"url": "https://notion.so/welcome"}
    blocks = {"results": [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Hello"}]}}], "has_more": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/pages/page1":
            return httpx.Response(200, json=page)
        if request.method == "GET" and path == "/v1/blocks/page1/children":
            return httpx.Response(200, json=blocks)
        if request.method == "POST" and path == "/v1/search":
            return httpx.Response(200, json=search)
        return httpx.Response(404)

    conn = _connector(handler)
    result = conn.scan({}, None)
    items = list(result.items)
    assert len(items) == 1
    assert items[0].remote_id == "page1"
    assert items[0].name == "Welcome"
    assert items[0].version == "2026-01-01T00:00:00Z"
    assert result.full is True

    fetched = conn.fetch({}, items[0])
    assert fetched.mime_type == "text/markdown"
    assert fetched.content == b"Hello\n"
    assert fetched.attributes["source_url"] == "https://notion.so/welcome"


def test_check_passes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/users/me"
        return httpx.Response(200, json={"type": "bot"})

    _connector(handler).check()
