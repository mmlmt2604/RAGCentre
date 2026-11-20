"""A reference ``ragcentre.vector_store/v1`` service over an in-memory brute-force index.

It stores collections as Python dicts, computes exact scores with ``ragcentre.vectors``, and
evaluates filters with ``ragcentre.models.filters.matches`` — the same reference evaluator the
contract suite uses. That makes it the smallest correct implementation of the protocol, and the
working example for `docs/protocols/vector-store-shim-guide.md`.

Run it with::

    uvicorn memory_shim:app --port 8702

Set ``MEMORY_SHIM_API_KEY`` (or pass ``api_key=`` to ``create_app``) to require
``Authorization: Bearer <key>`` on every request except ``GET /info``. Data lives in memory and
disappears on restart; swap the dict for your database's calls to wrap a real store.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from ragcentre.errors import InvalidFilter
from ragcentre.models.filters import FieldType, matches, parse_filter
from ragcentre.vectors import cosine_similarity, dot, l2_distance

PROTOCOL = "ragcentre.vector_store/v1"
METRICS = ("cosine", "l2", "dot")
FIELD_TYPES = ("str", "int", "float", "bool")
DEFAULT_MAX_BATCH = 1000
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class ApiError(Exception):
    """A failure with a protocol error response: HTTP ``status``, ``code`` and ``message``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _score(metric: str, a: list[float], b: list[float]) -> float:
    if metric == "cosine":
        return cosine_similarity(a, b)
    if metric == "l2":
        return -l2_distance(a, b)
    return dot(a, b)


@dataclass
class Collection:
    dim: int
    metric: str
    fields: dict[str, FieldType]
    records: dict[str, tuple[list[float], dict[str, Any]]] = field(default_factory=dict)


class MemoryShim:
    """The brute-force store behind the service. Replace its dicts with database calls to wrap a
    real vector database (see the guide's filter-translation section)."""

    def __init__(self, max_batch: int = DEFAULT_MAX_BATCH, max_dim: int | None = None) -> None:
        self.max_batch = max_batch
        self.max_dim = max_dim
        self._collections: dict[str, Collection] = {}
        self._lock = threading.RLock()

    def info(self, name: str, version: str) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "service_name": name,
            "service_version": version,
            "capabilities": {
                "metrics": list(METRICS),
                "max_batch": self.max_batch,
                "max_dim": self.max_dim,
                "consistency": "immediate",
                "filter_exact": True,
                "max_collections": None,
            },
        }

    def _get(self, name: str) -> Collection | None:
        return self._collections.get(name)

    def put(self, name: str, dim: int, metric: str, fields: dict[str, FieldType]) -> None:
        with self._lock:
            existing = self._get(name)
            if existing is None:
                if self.max_dim is not None and dim > self.max_dim:
                    raise ApiError(400, "invalid_request", f"dimension {dim} exceeds max_dim {self.max_dim}")
                self._collections[name] = Collection(dim, metric, dict(fields))
                return
            if existing.dim != dim or existing.metric != metric:
                raise ApiError(
                    409,
                    "collection_mismatch",
                    f"collection {name} exists with dim={existing.dim}, metric={existing.metric}; "
                    f"requested dim={dim}, metric={metric}",
                )
            for fname, ftype in fields.items():
                if fname in existing.fields and existing.fields[fname] != ftype:
                    raise ApiError(
                        409, "collection_mismatch", f"field {fname} is {existing.fields[fname]} in {name}, not {ftype}"
                    )
            existing.fields.update(fields)

    def info_collection(self, name: str) -> dict[str, Any]:
        col = self._get(name)
        if col is None:
            raise ApiError(404, "collection_not_found", f"collection {name} does not exist")
        return {"dim": col.dim, "metric": col.metric, "count": len(col.records)}

    def drop(self, name: str) -> None:
        with self._lock:
            self._collections.pop(name, None)

    def upsert(self, name: str, records: list[dict[str, Any]]) -> None:
        if len(records) > self.max_batch:
            raise ApiError(400, "invalid_request", f"batch of {len(records)} exceeds max_batch {self.max_batch}")
        col = self._get(name)
        if col is None:
            raise ApiError(404, "collection_not_found", f"collection {name} does not exist")
        with self._lock:
            for rec in records:
                rid = rec["id"]
                vector = rec["vector"]
                if len(vector) != col.dim:
                    raise ApiError(
                        400,
                        "invalid_request",
                        f"record {rid} has dimension {len(vector)}; collection {name} has {col.dim}",
                    )
                col.records[rid] = (list(vector), dict(rec["metadata"]))

    def delete(self, name: str, ids: list[str]) -> None:
        with self._lock:
            col = self._get(name)
            if col is None:
                return  # idempotent, like the Python interface
            for rid in ids:
                col.records.pop(rid, None)

    def query(self, name: str, vector: list[float], k: int, filter_raw: Any) -> dict[str, Any]:
        col = self._get(name)
        if col is None:
            raise ApiError(404, "collection_not_found", f"collection {name} does not exist")
        if len(vector) != col.dim:
            raise ApiError(
                400, "invalid_request", f"query vector has dimension {len(vector)}; collection {name} has {col.dim}"
            )
        schema = {**col.fields, "document_id": "str"}
        try:
            f = parse_filter(filter_raw, schema)
        except InvalidFilter as exc:
            raise ApiError(400, "invalid_request", str(exc)) from None
        with self._lock:
            records = list(col.records.items())
        scored = [(rid, _score(col.metric, vec, vector)) for rid, (vec, meta) in records if matches(f, meta)]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return {"matches": [{"id": rid, "score": score} for rid, score in scored[:k]]}


# ---- request validation ----------------------------------------------------------------------


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise ApiError(400, "invalid_request", "request body must be a JSON object") from None
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_request", "request body must be a JSON object")
    return body


def _check_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ApiError(400, "invalid_request", f"invalid collection name {name!r}")


def _positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApiError(400, "invalid_request", f"{key} must be a positive integer")
    return value


def _metric(value: Any) -> str:
    if value not in METRICS:
        raise ApiError(400, "invalid_request", f"metric must be one of {', '.join(METRICS)}")
    return value


def _fields(value: Any) -> dict[str, FieldType]:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_request", "fields must be an object")
    for fname, ftype in value.items():
        if not isinstance(fname, str) or ftype not in FIELD_TYPES:
            raise ApiError(400, "invalid_request", f"field {fname!r} must have a type in {', '.join(FIELD_TYPES)}")
    return dict(value)


def _numbers(value: Any, key: str) -> list[float]:
    if not isinstance(value, list) or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value):
        raise ApiError(400, "invalid_request", f"{key} must be a list of numbers")
    return [float(x) for x in value]


def _record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_request", "each record must be an object")
    rid = value.get("id")
    if not isinstance(rid, str):
        raise ApiError(400, "invalid_request", "each record needs a string 'id'")
    vector = _numbers(value.get("vector"), "record vector")
    metadata = value.get("metadata")
    if not isinstance(metadata, dict) or not all(
        isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in metadata.items()
    ):
        raise ApiError(400, "invalid_request", "record metadata must be a flat object of scalars")
    return {"id": rid, "vector": vector, "metadata": dict(metadata)}


# ---- app -------------------------------------------------------------------------------------


def create_app(
    *,
    name: str = "memory-shim",
    version: str = "0.1.0",
    api_key: str | None = None,
    max_batch: int = DEFAULT_MAX_BATCH,
    max_dim: int | None = None,
) -> FastAPI:
    """Build the service. ``api_key`` defaults to ``MEMORY_SHIM_API_KEY``; when set, every request
    except ``GET /info`` must carry ``Authorization: Bearer <value>``."""
    shim = MemoryShim(max_batch=max_batch, max_dim=max_dim)
    key = api_key if api_key is not None else (os.environ.get("MEMORY_SHIM_API_KEY") or None)

    app = FastAPI(title=f"{name} for RAGCentre", version="0.1.0")

    @app.middleware("http")
    async def check_key(request: Request, call_next: Any) -> Any:
        # /info is open: RAGCentre reads it without credentials when the provider is created.
        if key is not None and not (request.method == "GET" and request.url.path == "/info"):
            given = request.headers.get("authorization", "")
            if given != f"Bearer {key}":
                return error(401, "unauthorized", "missing or wrong bearer token")
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error(exc.status, exc.code, exc.message)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, __: Exception) -> JSONResponse:
        return error(500, "unavailable", "internal error")

    @app.get("/info")
    def info() -> dict[str, Any]:
        return shim.info(name, version)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.put("/collections/{name}", status_code=200)
    async def put_collection(name: str, request: Request) -> Response:
        _check_name(name)
        body = await _json_body(request)
        dim = _positive_int(body.get("dim"), "dim")
        metric = _metric(body.get("metric"))
        fields = _fields(body.get("fields"))
        shim.put(name, dim, metric, fields)
        return Response(status_code=200)

    @app.get("/collections/{name}")
    def get_collection(name: str) -> dict[str, Any]:
        _check_name(name)
        return shim.info_collection(name)

    @app.delete("/collections/{name}", status_code=204)
    def delete_collection(name: str) -> Response:
        _check_name(name)
        shim.drop(name)
        return Response(status_code=204)

    @app.post("/collections/{name}/upsert", status_code=204)
    async def upsert(name: str, request: Request) -> Response:
        _check_name(name)
        body = await _json_body(request)
        records = body.get("records")
        if not isinstance(records, list):
            raise ApiError(400, "invalid_request", "'records' must be a list")
        shim.upsert(name, [_record(r) for r in records])
        return Response(status_code=204)

    @app.post("/collections/{name}/delete", status_code=204)
    async def delete(name: str, request: Request) -> Response:
        _check_name(name)
        body = await _json_body(request)
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ApiError(400, "invalid_request", "'ids' must be a list of strings")
        shim.delete(name, ids)
        return Response(status_code=204)

    @app.post("/collections/{name}/query")
    async def query(name: str, request: Request) -> dict[str, Any]:
        _check_name(name)
        body = await _json_body(request)
        vector = _numbers(body.get("vector"), "vector")
        k = _positive_int(body.get("k"), "k")
        return shim.query(name, vector, k, body.get("filter"))

    return app
