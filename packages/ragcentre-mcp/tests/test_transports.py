"""Run ragcentre-mcp as a real process, over stdio and Streamable HTTP, against the fake API on a port."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator

import httpx
import httpx2
import pytest
import uvicorn
from fake_api import FakeApi
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from starlette.requests import Request
from starlette.responses import Response

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def ragcentre_url() -> Iterator[str]:
    fake = FakeApi()

    async def app(scope, receive, send):  # type: ignore[no-untyped-def]
        request = Request(scope, receive)
        body = await request.body()
        out = fake.handle(httpx.Request(request.method, str(request.url), headers=request.headers, content=body))
        await Response(out.content, status_code=out.status_code, media_type="application/json")(scope, receive, send)

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


async def test_stdio(ragcentre_url: str) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ragcentre_mcp", "--corpus", "manuals"],
        env={**os.environ, "RAGCENTRE_URL": ragcentre_url, "RAGCENTRE_API_TOKEN": "read-token"},
    )
    async with Client(params) as client:
        result = await client.call_tool("search", {"query": "torque", "top_k": 2})
    assert not result.is_error
    assert [r["chunk_id"] for r in result.structured_content["results"]] == ["k-d-valve-0", "k-d-valve-1"]


@pytest.fixture
def mcp_http_url(ragcentre_url: str) -> Iterator[str]:
    port = free_port()
    env = {**os.environ, "RAGCENTRE_URL": ragcentre_url, "RAGCENTRE_API_TOKEN": "t", "RAGCENTRE_MCP_TOKEN": "mcp-s3"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "ragcentre_mcp", "--transport", "http", "--port", str(port), "--log-level", "WARNING"],
        env=env,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(200):
            try:
                httpx.post(url, timeout=0.2)
                break
            except httpx.TransportError:
                time.sleep(0.05)
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


async def test_streamable_http_with_token(mcp_http_url: str) -> None:
    async with httpx2.AsyncClient() as anonymous:
        assert (await anonymous.post(mcp_http_url, json={})).status_code == 401
    async with (
        httpx2.AsyncClient(headers={"Authorization": "Bearer mcp-s3"}) as http,
        Client(streamable_http_client(mcp_http_url, http_client=http)) as client,
    ):
        result = await client.call_tool("fetch", {"id": "d-valve"})
    assert result.structured_content["kind"] == "document"
