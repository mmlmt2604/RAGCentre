"""Serve the built web UI (``src/ragcentre/ui_dist/``) at ``/``.

Files are served as they are; any other path that does not start with ``/v1`` gets
``index.html``, so client-side routes survive a page reload. ``/v1`` paths never reach the UI:
unknown API paths keep the API's JSON 404 (and its token check). The UI is not behind the API
token; the page itself asks for the token when the API requires one.

Without a build, ``/`` explains how to make one.
"""

from __future__ import annotations

from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, PlainTextResponse, Response
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


def _is_api(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


class SinglePageApp(StaticFiles):
    """Static files with an ``index.html`` fallback for client-side routes."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory=directory, html=True, check_dir=True)
        self.index = directory / "index.html"

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
        if path.startswith("assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers.setdefault("Cache-Control", "no-cache")
        return response


class UiApp:
    """Mounted at the root after the API routers; ``/v1`` paths fall through to a JSON 404."""

    def __init__(self, directory: Path | None = None) -> None:
        directory = UI_DIST if directory is None else directory
        self.spa = SinglePageApp(directory) if (directory / "index.html").is_file() else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope["path"]
        if scope["type"] != "http" or _is_api(path):
            raise HTTPException(status_code=404)
        if self.spa is not None:
            await self.spa(scope, receive, send)
            return
        status = 200 if path == "/" else 404
        await PlainTextResponse(MISSING_MESSAGE, status_code=status)(scope, receive, send)
