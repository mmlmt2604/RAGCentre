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
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_core import ErrorDetails

PROTOCOL = "ragcentre.parser/v1"

# (content, filename, options) -> Document IR; ``options`` is an instance of the app's options model
ParseFunction = Callable[[bytes, str, Any], dict[str, Any]]


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


class ShimOptions(BaseModel):
    """Base class for a shim's options. Unknown keys are rejected and types are strict, so ``"false"``
    is not a boolean and ``true`` is not an integer. Give every field a ``description``: it is the help
    text in the options schema that ``/info`` reports."""

    model_config = ConfigDict(extra="forbid", strict=True)


def _invalid(message: str) -> ShimError:
    return ShimError(400, "invalid_options", message)


def _bounds(model: type[BaseModel], key: str) -> dict[str, Any]:
    """The ``ge``/``gt``/``le``/``lt`` constraints declared on field ``key``."""
    field = model.model_fields.get(key)
    found: dict[str, Any] = {}
    for item in field.metadata if field else []:
        for name in ("ge", "gt", "le", "lt"):
            if hasattr(item, name):
                found[name] = getattr(item, name)
    return found


def _range(noun: str, bounds: dict[str, Any]) -> str:
    low = bounds.get("ge", bounds.get("gt"))
    high = bounds.get("le", bounds.get("lt"))
    if "ge" in bounds and "le" in bounds:
        return f"{noun} from {low} to {high}"
    parts = []
    if "ge" in bounds:
        parts.append(f"at least {low}")
    if "gt" in bounds:
        parts.append(f"greater than {low}")
    if "le" in bounds:
        parts.append(f"at most {high}")
    if "lt" in bounds:
        parts.append(f"less than {high}")
    return f"{noun} that is {' and '.join(parts)}" if parts else noun


def _expectation(model: type[BaseModel], error: ErrorDetails) -> str:
    kind = error["type"]
    ctx = error.get("ctx") or {}
    key = str(error["loc"][0]) if error["loc"] else ""
    bounds = _bounds(model, key)
    if kind == "bool_type":
        return "true or false"
    if kind == "literal_error":
        return str(ctx.get("expected", "one of the allowed values")).replace("'", "")
    if kind in ("int_type", "int_parsing", "int_from_float"):
        return _range("an integer", bounds)
    if kind in ("float_type", "float_parsing"):
        return _range("a number", bounds)
    if kind in ("greater_than_equal", "greater_than", "less_than_equal", "less_than"):
        field = model.model_fields.get(key)
        is_int = field is not None and "int" in str(field.annotation) and "float" not in str(field.annotation)
        return _range("an integer" if is_int else "a number", bounds)
    simple = {"string_type": "a string", "list_type": "a list", "tuple_type": "a list", "dict_type": "an object"}
    return simple.get(kind, str(error["msg"]))


def options_message(model: type[BaseModel], exc: ValidationError) -> str:
    """A short sentence about the first option that failed validation, written from the error's
    ``loc``, ``type`` and ``ctx`` (not pydantic's own text)."""
    errors = exc.errors()
    first = errors[0]
    top = first["loc"][0] if first["loc"] else None
    key = str(top) if top is not None else "options"
    if first["type"] == "extra_forbidden":
        return f"option {key!r} is not recognised"
    if first["type"] == "missing":
        return f"option {key!r} is required"
    # A union field reports one error per branch; describe them together.
    same = [e for e in errors if e["loc"] and e["loc"][0] == top and e["type"] != "extra_forbidden"]
    expected = " or ".join(dict.fromkeys(_expectation(model, e) for e in same))
    return f"option {key!r} must be {expected}"


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
    options_model: type[BaseModel],
    serialise: bool = False,
    shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """Build the service.

    ``options_model`` validates the ``options`` object of every parse request (unknown keys and wrong
    types are ``400 invalid_options``) and its JSON Schema is reported by ``/info`` as
    ``options_schema``. ``version`` is called on every ``/info`` request. With ``serialise``, calls to ``parse`` run
        one at a time (for engines that cannot be used concurrently). When the environment variable
    ``api_key_env`` is set, every request except ``GET /info`` must carry
        ``Authorization: Bearer <value>``. ``shutdown`` runs when the server stops (to release an
        engine's child processes).
    """
    api_key = os.environ.get(api_key_env) or None
    lock = threading.Lock() if serialise else None
    schema = options_model.model_json_schema()

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
            "options_schema": schema,
        }

    @app.post("/parse", response_model=None)
    def parse_endpoint(file: UploadFile = File(...), options: str = Form("{}")) -> dict[str, Any] | JSONResponse:
        filename = file.filename or "document"
        try:
            raw = _read_options(options)
            try:
                opts = options_model.model_validate(raw)
            except ValidationError as exc:
                raise _invalid(options_message(options_model, exc)) from None
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
