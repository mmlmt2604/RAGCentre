from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx

from ragcentre.connectors.sharepoint import GraphConfig, SharePointConnector
from ragcentre.providers.base import BuildEnv


def _env(handler: Any) -> BuildEnv:
    transport = httpx.MockTransport(handler)

    def http(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=transport, **kwargs)

    return BuildEnv("p", "sharepoint", http, Path("."), threading.BoundedSemaphore(1))


def _connector(handler: Any) -> SharePointConnector:
    return SharePointConnector(GraphConfig(tenant_id="t", client_id="c"), {"client_secret": "s"}, _env(handler))


def test_scan_and_fetch() -> None:
    delta = {
        "value": [
            {
                "id": "item1",
                "name": "a.txt",
                "file": {"mimeType": "text/plain"},
                "size": 5,
                "cTag": "ctag1",
                "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                "parentReference": {"path": "/drive/root:/docs"},
            },
            {"id": "item2", "deleted": {}},
        ],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/d/root/delta?token=delta-tok",
    }
    item_meta = {"@microsoft.graph.downloadUrl": "https://cdn.test/a.txt", "webUrl": "https://contoso.test/a.txt"}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/oauth2/v2.0/token" in request.url.path:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.url.path.endswith("/root/delta"):
            return httpx.Response(200, json=delta)
        if request.url.path.endswith("/items/item1"):
            return httpx.Response(200, json=item_meta)
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"})
        return httpx.Response(404)

    conn = _connector(handler)
    result = conn.scan({"drive_id": "d"}, None)
    items = list(result.items)
    assert len(items) == 2
    assert items[0].remote_id == "item1"
    assert items[0].version == "ctag1"
    assert items[1].deleted is True
    assert result.full is False
    assert result.next_cursor is None
    assert result.resume_cursor.endswith("token=delta-tok")

    fetched = conn.fetch({"drive_id": "d"}, items[0])
    assert fetched.content == b"hello"
    assert fetched.mime_type == "text/plain"
    assert fetched.attributes["source_url"] == "https://contoso.test/a.txt"


def test_check_fetches_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/oauth2/v2.0/token" in request.url.path
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    _connector(handler).check()
