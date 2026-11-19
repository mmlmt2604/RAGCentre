"""The FastAPI app factory, auth, option parsing and upload handling shared by the shims.

    GET  /info   service name, version and supported MIME types
    POST /parse  multipart ``file`` and ``options`` (JSON) -> Document IR

With ``jobs=True`` it also offers ``POST /parse?mode=async`` and ``/jobs/{id}`` (see ``create_app``).

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
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

if TYPE_CHECKING:
    from shimkit.options import Option

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


# --- async jobs -------------------------------------------------------------------------------

JOB_TTL_SECONDS = 3600.0
POLL_AFTER_SECONDS = 2


@dataclass
class _Job:
    """One async parse. ``status`` moves ``running`` -> ``succeeded`` | ``failed``."""

    id: str
    status: str = "running"
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None  # {"http_status", "code", "message"}
    created: float = field(default_factory=time.monotonic)
    finished: float | None = None


class JobTable:
    """In-memory async jobs for ``POST /parse?mode=async`` (parser protocol 1.1).

    Jobs run on a thread pool of ``workers`` threads, and finished jobs are dropped ``ttl`` seconds
    after they finish. A restart loses every job: RAGCentre sees ``404`` and submits again.
    """

    def __init__(self, workers: int, ttl: float = JOB_TTL_SECONDS) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="shim-job")
        self._ttl = ttl
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()

    def submit(self, run: Callable[[], dict[str, Any]]) -> _Job:
        job = _Job(id=uuid.uuid4().hex)
        with self._lock:
            self._expire()
            self._jobs[job.id] = job
        self._pool.submit(self._run, job, run)
        return job

    def _run(self, job: _Job, run: Callable[[], dict[str, Any]]) -> None:
        try:
            result = run()
            error = None
        except ShimError as exc:
            result, error = None, {"http_status": exc.status, "code": exc.code, "message": exc.message}
        except Exception as exc:
            result = None
            error = {"http_status": 422, "code": "conversion_failed", "message": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            job.result, job.error = result, error
            job.finished = time.monotonic()
            job.status = "failed" if error else "succeeded"

    def get(self, job_id: str) -> _Job | None:
        with self._lock:
            self._expire()
            return self._jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def _expire(self) -> None:
        now = time.monotonic()
        for job_id in [j.id for j in self._jobs.values() if j.finished is not None and now - j.finished > self._ttl]:
            del self._jobs[job_id]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


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
    options: Sequence[Option] | None = None,
    strict_options: bool = True,
    jobs: bool = False,
    job_workers: int = 2,
    job_ttl: float = JOB_TTL_SECONDS,
) -> FastAPI:
    """Build the service.

        ``version`` is called on every ``/info`` request. With ``serialise``, calls to ``parse`` run
        one at a time (for engines that cannot be used concurrently). When the environment variable
    ``api_key_env`` is set, every request except ``GET /info`` must carry
        ``Authorization: Bearer <value>``. ``shutdown`` runs when the server stops (to release an
        engine's child processes).

    ``options`` declares the route options (see ``shimkit.options``): ``/info`` reports them as
    ``options_schema``, and a request with an option that is not declared is a ``400
    invalid_options`` unless ``strict_options`` is False. Your ``parse`` function receives the
    options object as the client sent it; read it with ``shimkit.parse_options``.

    With ``jobs`` the service also speaks the async mode of the parser protocol: ``/info`` reports
    ``capabilities.async``, ``POST /parse?mode=async`` (or ``Prefer: respond-async``) answers
    ``202 {"job_id", "poll_after_seconds"}``, and ``GET /jobs/{id}``, ``GET /jobs/{id}/result`` and
    ``DELETE /jobs/{id}`` report, return and drop the job. Jobs run on ``job_workers`` threads (one
    with ``serialise``) and are dropped ``job_ttl`` seconds after they finish. Without ``jobs`` the
    service is sync-only, as before.
    """
    from shimkit.options import options_schema, parse_options  # not at the top: options imports this module

    api_key = os.environ.get(api_key_env) or None
    lock = threading.Lock() if serialise else None
    table = JobTable(1 if serialise else job_workers, job_ttl) if jobs else None
    advertised = {**capabilities, **({"async": True} if jobs else {})}
    declared = list(options) if options is not None else None

    def run_parse(content: bytes, filename: str, opts: dict[str, Any]) -> dict[str, Any]:
        if lock is None:
            return parse(content, filename, opts)
        with lock:
            return parse(content, filename, opts)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if table is not None:
            table.shutdown()
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
        body: dict[str, Any] = {
            "protocol": PROTOCOL,
            "name": name,
            "version": version(),
            "mime_types": list(mime_types),
            "capabilities": dict(advertised),
        }
        if declared is not None:
            body["options_schema"] = options_schema(declared)
        return body

    @app.post("/parse", response_model=None)
    def parse_endpoint(
        request: Request, file: UploadFile = File(...), options: str = Form("{}")
    ) -> dict[str, Any] | JSONResponse:
        filename = file.filename or "document"
        wants_async = request.query_params.get("mode") == "async" or "respond-async" in request.headers.get(
            "prefer", ""
        )
        try:
            opts = _read_options(options)
            if declared is not None and strict_options:
                parse_options(declared, opts)  # rejects an unknown or invalid option before any work
            content = file.file.read()
            if wants_async and table is not None:
                job = table.submit(lambda: run_parse(content, filename, opts))
                return JSONResponse({"job_id": job.id, "poll_after_seconds": POLL_AFTER_SECONDS}, status_code=202)
            return run_parse(content, filename, opts)
        except ShimError as exc:
            return error_response(exc.status, exc.code, exc.message)
        except Exception as exc:
            return error_response(422, "conversion_failed", f"{filename}: {type(exc).__name__}: {exc}")

    if table is not None:

        @app.get("/jobs/{job_id}", response_model=None)
        def job_status(job_id: str) -> dict[str, Any] | JSONResponse:
            job = table.get(job_id)
            if job is None:
                return error_response(404, "job_not_found", "unknown or expired job")
            body: dict[str, Any] = {"status": job.status}
            if job.error is not None:
                body["error"] = job.error
            return body

        @app.get("/jobs/{job_id}/result", response_model=None)
        def job_result(job_id: str) -> dict[str, Any] | JSONResponse:
            job = table.get(job_id)
            if job is None:
                return error_response(404, "job_not_found", "unknown or expired job")
            if job.status == "running":
                return error_response(409, "job_running", "the job has not finished")
            if job.error is not None:
                return error_response(int(job.error["http_status"]), job.error["code"], job.error["message"])
            assert job.result is not None
            return job.result

        @app.delete("/jobs/{job_id}", status_code=204)
        def job_delete(job_id: str) -> Response:
            table.delete(job_id)
            return Response(status_code=204)

    return app
