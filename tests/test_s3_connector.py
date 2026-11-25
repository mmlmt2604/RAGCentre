from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx

from ragcentre.connectors.base import RemoteItem
from ragcentre.connectors.s3 import EMPTY_SHA256, S3Config, S3Connector, sign_headers
from ragcentre.providers.base import BuildEnv

LIST_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>b</Name>
  <IsTruncated>false</IsTruncated>
  <Contents>
    <Key>invoices/a.txt</Key>
    <ETag>"abc123"</ETag>
    <Size>11</Size>
    <LastModified>2026-01-01T00:00:00.000Z</LastModified>
  </Contents>
</ListBucketResult>"""


def test_sign_headers_shape() -> None:
    headers = sign_headers(
        method="GET",
        host="b.s3.us-east-1.amazonaws.com",
        path="/a.txt",
        query=[("list-type", "2"), ("prefix", "x")],
        region="us-east-1",
        access_key="AKID",
        secret_key="SECRET",
        session_token=None,
        payload_hash=EMPTY_SHA256,
    )
    assert headers["x-amz-content-sha256"] == EMPTY_SHA256
    assert headers["x-amz-date"].endswith("Z")
    auth = headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKID/")
    assert "/us-east-1/s3/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=" in auth
    assert "x-amz-security-token" not in headers


def _connector(handler: Any) -> S3Connector:
    transport = httpx.MockTransport(handler)

    def http(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=transport, **kwargs)

    env = BuildEnv(
        provider_id="p",
        provider_name="s3",
        http=http,
        data_dir=Path("."),
        semaphore=threading.BoundedSemaphore(1),
    )
    config = S3Config(region="us-east-1", path_style=True, base_url="http://s3.test")
    return S3Connector(config, {"access_key_id": "AKID", "secret_access_key": "SECRET"}, env)


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("authorization", "").startswith("AWS4-HMAC-SHA256")
    path = request.url.path
    query = request.url.query.decode()
    if path == "/":
        return httpx.Response(200, content=b"")
    if path == "/b/" and "list-type" in query:
        return httpx.Response(200, content=LIST_XML, headers={"content-type": "application/xml"})
    if path == "/b/invoices/a.txt":
        return httpx.Response(
            200, content=b"hello world", headers={"content-type": "application/octet-stream", "etag": '"abc123"'}
        )
    return httpx.Response(404)


def test_check_passes() -> None:
    _connector(_handler).check()


def test_scan_parses_listing() -> None:
    result = _connector(_handler).scan({"bucket": "b", "prefix": "invoices/"}, None)
    items = list(result.items)
    assert len(items) == 1
    item = items[0]
    assert item.remote_id == "invoices/a.txt"
    assert item.name == "a.txt"
    assert item.version == '"abc123"'
    assert item.size == 11
    assert result.full is True
    assert result.next_cursor is None


def test_fetch_returns_bytes_and_mime_fallback() -> None:
    fetched = _connector(_handler).fetch(
        {"bucket": "b"},
        RemoteItem(
            remote_id="invoices/a.txt",
            name="a.txt",
            mime_type=None,
            size=11,
            version='"abc123"',
            modified_at="2026-01-01T00:00:00.000Z",
            deleted=False,
            path="invoices/a.txt",
        ),
    )
    assert fetched.content == b"hello world"
    assert fetched.mime_type == "text/plain"  # content-type was octet-stream; guessed from extension
    assert fetched.attributes["s3_key"] == "invoices/a.txt"
    assert fetched.attributes["source_url"] == "s3://b/invoices/a.txt"
