from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message
from typer.testing import CliRunner

from ragcentre.api.app import create_app
from ragcentre.cli import app as cli
from ragcentre.context import open_context
from ragcentre.settings import Settings

AUTH = {"Authorization": "Bearer test-token"}

# Routes reachable without credentials: health, and the auth endpoints that let a browser in.
PUBLIC_ROUTES = {"/v1/health", "/v1/auth/config", "/v1/auth/whoami", "/v1/auth/setup", "/v1/auth/login"}


@pytest.mark.parametrize("mode", ["direct", "root_path", "mount"])
def test_public_and_protected_routes(tmp_path: Path, mode: str) -> None:
    ctx = open_context(Settings(data_dir=tmp_path, api_token="test-token"))
    try:
        app = create_app(ctx, ui_dir=tmp_path / "missing")
        schema = app.openapi()
        prefix = "" if mode == "direct" else "/rag"
        if mode == "mount":
            outer = FastAPI()
            outer.mount(prefix, app)
            app = outer
        with TestClient(app, root_path=prefix if mode == "root_path" else "") as client:
            assert client.get(prefix + "/").status_code == 200
            assert client.get(prefix + "/v1/health").status_code == 200
            assert client.post(prefix + "/v1/health").status_code == 401
            for path, methods in schema["paths"].items():
                if path in PUBLIC_ROUTES:
                    continue
                for method in methods:
                    assert client.request(method, prefix + path).status_code == 401, (method, path)
            for path in ("/v1/docs", "/v1/docs/oauth2-redirect", "/v1/openapi.json"):
                assert client.get(prefix + path).status_code == 401
                assert client.get(prefix + path, headers=AUTH).status_code == 200
            assert client.get(prefix + "/v1/unknown", headers=AUTH).status_code == 404
            assert client.get(prefix + "/v1%2fhealth").status_code == 401
    finally:
        ctx.close()


@pytest.mark.parametrize(
    ("authorized", "length", "status", "expected_reads"),
    [(False, b"1073741824", 401, 0), (True, b"1073741824", 413, 0), (True, None, 413, 2)],
)
def test_request_body_is_bounded(
    tmp_path: Path, authorized: bool, length: bytes | None, status: int, expected_reads: int
) -> None:
    ctx = open_context(Settings(data_dir=tmp_path, api_token="test-token", max_upload_mb=1))
    headers = [(b"content-type", b"multipart/form-data; boundary=limit")]
    if authorized:
        headers.append((b"authorization", b"Bearer test-token"))
    if length is not None:
        headers.append((b"content-length", length))

    reads = 0
    messages: list[Message] = []

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        assert reads <= expected_reads
        start = b'--limit\r\nContent-Disposition: form-data; name="file"; filename="file.txt"\r\n\r\n'
        return {
            "type": "http.request",
            "body": start + b"x" * (600 * 1024) if reads == 1 else b"x" * (600 * 1024),
            "more_body": True,
        }

    async def send(message: Message) -> None:
        messages.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/v1/corpora/example/documents",
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
    }
    try:
        asyncio.run(create_app(ctx)(scope, receive, send))
    finally:
        ctx.close()
    assert reads == expected_reads
    assert messages[0]["status"] == status
    assert json.loads(messages[1]["body"])["error"]["code"] == (
        "unauthorized" if status == 401 else "payload_too_large"
    )


def test_serve_refuses_public_bind_without_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RAGCENTRE_API_TOKEN", raising=False)
    monkeypatch.setenv("RAGCENTRE_DATA_DIR", str(tmp_path / "data"))
    result = CliRunner().invoke(cli, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "RAGCENTRE_API_TOKEN" in result.output
    assert not (tmp_path / "data").exists()
