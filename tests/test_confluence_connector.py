from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx

from ragcentre.connectors.confluence import ConfluenceConfig, ConfluenceConnector, _storage_to_markdown
from ragcentre.providers.base import BuildEnv


def _env(handler: Any) -> BuildEnv:
    transport = httpx.MockTransport(handler)

    def http(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=transport, **kwargs)

    return BuildEnv("p", "confluence", http, Path("."), threading.BoundedSemaphore(1))


def _connector(handler: Any) -> ConfluenceConnector:
    config = ConfluenceConfig(base_url="https://team.atlassian.net")
    return ConfluenceConnector(config, {"email": "u@x", "api_token": "tok"}, _env(handler))


def test_storage_to_markdown() -> None:
    html = "<h1>Hi</h1><p>Hello <strong>world</strong></p><ul><li>one</li><li>two</li></ul>"
    assert _storage_to_markdown(html) == "# Hi\n\nHello **world**\n\n- one\n- two\n"


def test_scan_and_fetch() -> None:
    storage = "<h1>Home</h1><p>Body</p>"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/wiki/api/v2/spaces" and "keys=TEAM" in request.url.query.decode():
            return httpx.Response(200, json={"results": [{"id": "space1"}]})
        if path == "/wiki/api/v2/spaces/space1/pages":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "p1",
                            "title": "Home",
                            "status": "current",
                            "version": {"number": 3, "createdAt": "2026-01-01T00:00:00Z"},
                        }
                    ],
                    "_links": {},
                },
            )
        if path == "/wiki/api/v2/pages/p1":
            return httpx.Response(200, json={"body": {"storage": {"value": storage}}})
        return httpx.Response(404)

    conn = _connector(handler)
    result = conn.scan({"space_key": "TEAM"}, None)
    items = list(result.items)
    assert len(items) == 1
    assert items[0].remote_id == "p1"
    assert items[0].version == "3"
    assert result.full is True
    assert result.next_cursor is None

    fetched = conn.fetch({"space_key": "TEAM"}, items[0])
    assert fetched.mime_type == "text/markdown"
    assert fetched.content.startswith(b"# Home")
    assert fetched.attributes["space_key"] == "TEAM"
    assert fetched.attributes["source_url"].endswith("/wiki/spaces/TEAM/pages/p1")


def test_check_passes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wiki/api/v2/spaces"
        return httpx.Response(200, json={"results": []})

    _connector(handler).check()
