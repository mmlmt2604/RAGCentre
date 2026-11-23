from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ragcentre_mcp import cli


def _app() -> Starlette:
    return Starlette(routes=[Route("/mcp", lambda r: PlainTextResponse("ok"), methods=["POST"])])


def test_bearer_guard() -> None:
    client = TestClient(cli.BearerGuard(_app(), "s3cret"))
    assert client.post("/mcp").status_code == 401
    assert client.post("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
    resp = client.post("/mcp", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200 and resp.text == "ok"
    assert client.post("/mcp").headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("10.0.0.5", False),
        ("rag.example", False),
    ],
)
def test_is_loopback(host: str, loopback: bool) -> None:
    assert cli.is_loopback(host) is loopback


def test_public_http_needs_mcp_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("RAGCENTRE_MCP_TOKEN", raising=False)
    with pytest.raises(SystemExit) as info:
        cli.main(["--transport", "http", "--host", "0.0.0.0"])
    assert info.value.code == 2
    err = capsys.readouterr()
    assert "RAGCENTRE_MCP_TOKEN" in err.err and err.out == ""


def test_argument_limits() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--max-chars", "10"])
    with pytest.raises(SystemExit):
        cli.parse_args(["--default-top-k", "0"])
