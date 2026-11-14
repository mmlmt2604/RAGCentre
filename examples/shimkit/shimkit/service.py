"""The FastAPI app factory, auth, option parsing and upload handling shared by the shims.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

A shim supplies a ``parse(content, filename, options) -> IR dict`` function. It raises
:class:`ShimError` for failures it can classify; any other exception becomes
``422 conversion_failed``.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

PROTOCOL = "ragcentre.parser/v1"

# (content, filename, options) -> Document IR
ParseFunction = Callable[[bytes, str, dict[str, Any]], dict[str, Any]]


class ShimError(Exception):
    """A failure with a protocol error response: HTTP ``status``, ``code`` and ``message``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


# --- options ----------------------------------------------------------------------------------


def _invalid(message: str) -> ShimError:
    return ShimError(400, "invalid_options", message)


def bool_option(options: Mapping[str, Any], key: str, default: bool) -> bool:
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise _invalid(f"option {key!r} must be true or false")
    return value


def choice_option(options: Mapping[str, Any], key: str, allowed: Sequence[str], default: str) -> str:
    value = options.get(key, default)
    if not isinstance(value, str) or value not in allowed:
        raise _invalid(f"option {key!r} must be one of: {', '.join(allowed)}")
    return value


def int_option(options: Mapping[str, Any], key: str, default: int, *, minimum: int, maximum: int) -> int:
    value = options.get(key, default)
    # bool is an int subclass; ``true`` is not a number of dots per inch.
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _invalid(f"option {key!r} must be an integer from {minimum} to {maximum}")
    return value


# --- uploads ----------------------------------------------------------------------------------


def file_extension(filename: str) -> str:
    """The upload's extension, lower-cased and reduced to ``[a-z0-9]`` (at most 16 characters).

    ``""`` when there is none. Use it to check a file type before calling an engine."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return re.sub(r"[^a-z0-9]", "", name.rsplit(".", 1)[1].lower())[:16]


@contextmanager
def temp_upload(content: bytes, filename: str) -> Iterator[Path]:
    """Write ``content`` to a temporary file that keeps the upload's extension, and remove it
    afterwards. The path never contains any part of the client's filename except the sanitised
    extension (see :func:`file_extension`)."""
    suffix = f".{ext}" if (ext := file_extension(filename)) else ""
    with tempfile.TemporaryDirectory(prefix="shim-") as directory:
        path = Path(directory) / f"upload{suffix}"
        path.write_bytes(content)
        yield path


# --- app --------------------------------------------------------------------------------------


def _read_options(raw: str) -> dict[str, Any]:
    try:
        options = json.loads(raw or "{}")
    except json.JSONDecodeError:
        raise _invalid("options must be a JSON object") from None
    if not isinstance(options, dict):
        raise _invalid("options must be a JSON object")
    return options


def create_app(
    *,
    name: str,
    version: Callable[[], str],
    mime_types: Sequence[str],
    capabilities: Mapping[str, Any],
    api_key_env: str,
    parse: ParseFunction,
    serialise: bool = False,
    shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """Build the service.

        ``version`` is called on every ``/info`` request. With ``serialise``, calls to ``parse`` run
        one at a time (for engines that cannot be used concurrently). When the environment variable
    ``api_key_env`` is set, every request except ``GET /info`` must carry
        ``Authorization: Bearer <value>``. ``shutdown`` runs when the server stops (to release an
        engine's child processes).
    """
    api_key = os.environ.get(api_key_env) or None
    lock = threading.Lock() if serialise else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if shutdown is not None:
            shutdown()

    app = FastAPI(title=f"{name} for RAGCentre", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def check_key(request: Request, call_next: Any) -> Any:
        # /info is open: RAGCentre reads it without credentials when it creates the provider, to pin
        # the version (docs/protocols/parser-v1.md). It reveals only the name, version and MIME types.
        if api_key is not None and not (request.method == "GET" and request.url.path == "/info"):
            given = request.headers.get("authorization", "")
            if not hmac.compare_digest(given.encode(), f"Bearer {api_key}".encode()):
                return error_response(401, "unauthorized", "missing or wrong bearer token")
        return await call_next(request)

    @app.get("/info")
    def info() -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "name": name,
            "version": version(),
            "mime_types": list(mime_types),
            "capabilities": dict(capabilities),
        }

    @app.post("/parse", response_model=None)
    def parse_endpoint(file: UploadFile = File(...), options: str = Form("{}")) -> dict[str, Any] | JSONResponse:
        filename = file.filename or "document"
        try:
            opts = _read_options(options)
            content = file.file.read()
            if lock is None:
                return parse(content, filename, opts)
            with lock:
                return parse(content, filename, opts)
        except ShimError as exc:
            return error_response(exc.status, exc.code, exc.message)
        except Exception as exc:
            return error_response(422, "conversion_failed", f"{filename}: {type(exc).__name__}: {exc}")

    return app
