"""Serve the built web UI (``src/ragcentre/ui_dist/``) at ``/``.

Files are served as they are; any other path that does not start with ``/v1`` gets
``index.html``, so client-side routes survive a page reload. ``/v1`` paths never reach the UI:
unknown API paths keep the API's JSON 404 (and its token check). The UI is not behind the API
token; the page itself asks for the token when the API requires one.

Without a build, ``/`` explains how to make one.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from starlette._utils import get_route_path
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

UI_DIST = Path(__file__).resolve().parent.parent / "ui_dist"

MISSING_MESSAGE = (
    "RAGCentre's API is running at /v1 (documentation at /v1/docs), but the web UI has not been built.\n"
    "\n"
    "Build it from a source checkout with:\n"
    "\n"
    "    cd ui && npm ci && npm run build\n"
    "\n"
    "which writes src/ragcentre/ui_dist/. Then restart `ragcentre serve`.\n"
)


def is_api_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


class SinglePageApp(StaticFiles):
    """Static files with an ``index.html`` fallback for client-side routes."""

    def __init__(self, directory: Path, *, base_path: str = "") -> None:
        super().__init__(directory=directory, html=True, check_dir=True)
        self.index = directory / "index.html"
        self.base_path = base_path

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or scope["method"] not in ("GET", "HEAD"):
                raise
            # Unknown path: a client-side route. Missing files under /assets stay 404, so a stale
            # page asking for an old bundle fails visibly instead of receiving HTML.
            if path.startswith("assets/"):
                raise
            response = FileResponse(self.index)
        if self.base_path and isinstance(response, FileResponse) and Path(response.path) == self.index:
            content = await run_in_threadpool(self.index.read_text, encoding="utf-8")
            prefix = escape(self.base_path, quote=True)
            for attribute in ("src", "href"):
                for quote in ('"', "'"):
                    for asset in ("assets/", "favicon.svg"):
                        content = content.replace(
                            f"{attribute}={quote}/{asset}", f"{attribute}={quote}{prefix}/{asset}"
                        )
            meta = f'<meta name="ragcentre-base-path" content="{prefix}">'
            content = content.replace("<head>", f"<head>{meta}", 1) if "<head>" in content else meta + content
            response = HTMLResponse(content, status_code=response.status_code)
        if path.startswith("assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers.setdefault("Cache-Control", "no-cache")
        return response


class UiApp:
    """Mounted at the root after the API routers; ``/v1`` paths fall through to a JSON 404."""

    def __init__(self, directory: Path | None = None, *, base_path: str = "") -> None:
        directory = UI_DIST if directory is None else directory
        self.spa = SinglePageApp(directory, base_path=base_path) if (directory / "index.html").is_file() else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            raise HTTPException(status_code=404)
        path = get_route_path(scope)
        if is_api_path(path):
            raise HTTPException(status_code=404)
        if self.spa is not None:
            await self.spa(scope, receive, send)
            return
        status = 200 if path == "/" else 404
        await PlainTextResponse(MISSING_MESSAGE, status_code=status)(scope, receive, send)
