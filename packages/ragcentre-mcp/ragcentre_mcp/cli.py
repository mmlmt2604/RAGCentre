"""``ragcentre-mcp``: run the server over stdio (local clients) or Streamable HTTP."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import logging
import os
import sys
from collections.abc import Sequence

import anyio
from starlette.types import ASGIApp, Receive, Scope, Send

from ragcentre_mcp import __version__
from ragcentre_mcp.api import RagcentreApi
from ragcentre_mcp.server import Options, StartupError, build_server, startup

DEFAULT_URL = "http://127.0.0.1:8700"


class BearerGuard:
    """Every HTTP request needs ``Authorization: Bearer <RAGCENTRE_MCP_TOKEN>``."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            scheme, _, supplied = headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), self.token):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ragcentre-mcp",
        description="A read-only MCP server for a RAGCentre knowledge base. Connects to RAGCENTRE_URL "
        "with RAGCENTRE_API_TOKEN (use the server's read-only token).",
    )
    p.add_argument("--version", action="version", version=f"ragcentre-mcp {__version__}")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1", help="HTTP transport: address to listen on.")
    p.add_argument("--port", type=int, default=8710, help="HTTP transport: port to listen on.")
    p.add_argument(
        "--corpus", action="append", default=[], metavar="NAME", help="Expose only this corpus (repeatable)."
    )
    p.add_argument("--default-top-k", type=int, default=8, metavar="N")
    p.add_argument("--max-chars", type=int, default=12_000, metavar="N", help="Text budget per tool result.")
    p.add_argument("--allow-full-token", action="store_true", help="Run even if the API token has full access.")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)
    if not 1 <= args.default_top_k <= 50:
        p.error("--default-top-k must be between 1 and 50")
    if args.max_chars < 200:
        p.error("--max-chars must be at least 200")
    return args


async def _run(args: argparse.Namespace) -> None:
    mcp_token = os.environ.get("RAGCENTRE_MCP_TOKEN") or None
    if args.transport == "http" and not mcp_token and not is_loopback(args.host):
        raise StartupError(
            f"Listening on {args.host} needs RAGCENTRE_MCP_TOKEN, which clients must send as a bearer token."
        )
    options = Options(
        corpora=tuple(args.corpus),
        default_top_k=args.default_top_k,
        max_chars=args.max_chars,
        allow_full_token=args.allow_full_token,
    )
    api = RagcentreApi(os.environ.get("RAGCENTRE_URL") or DEFAULT_URL, os.environ.get("RAGCENTRE_API_TOKEN") or None)
    try:
        catalog = await startup(api, options)
        server = build_server(api, catalog, options)
        if args.transport == "stdio":
            await server.run_stdio_async()
            return
        import uvicorn

        app: ASGIApp = server.streamable_http_app(stateless_http=True, json_response=True, host=args.host)
        if mcp_token:
            app = BearerGuard(app, mcp_token)
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level=args.log_level.lower())
        await uvicorn.Server(config).serve()
    finally:
        await api.aclose()


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    # stdout carries the stdio transport, so logs go to stderr.
    logging.basicConfig(stream=sys.stderr, level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    try:
        anyio.run(_run, args)
    except StartupError as exc:
        print(f"ragcentre-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
