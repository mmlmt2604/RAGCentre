"""HTTP vector store for services speaking ``ragcentre.vector_store/v1`` (plan V3).

The service owns the collection shape and does the search; this client only maps the eight-method
``VectorStore`` interface onto the protocol's endpoints. Capabilities are read from ``GET /info``
when the store is created and captured for the instance, like a parser's service fields. Filters
are sent in RAGCentre's own JSON language (``to_json``), so the core never learns a vendor dialect.

Writes are retried with backoff (the service is the source of truth and creation is idempotent);
reads — ``/health``, collection info and queries — are never retried, because a degraded answer
that looks complete is worse than an error (plan §20).

``httpx`` is a core dependency, so this store needs no optional extra.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import Filter, to_json
from ragcentre.providers.base import ServiceInfo, parse_retry_after
from ragcentre.stores.base import (
    METRICS,
    Capabilities,
    CollectionInfo,
    CollectionNotFound,
    CollectionSpec,
    Match,
    Metric,
    Record,
    StoreError,
    check_collection_name,
    check_spec,
)
from ragcentre.stores.registry import VectorStoreType

PROTOCOL = "ragcentre.vector_store/v1"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_BATCH = 1000
MAX_ATTEMPTS = 3
BASE_BACKOFF = 0.25
MAX_BACKOFF = 5.0
RETRYABLE_STATUS = frozenset({408, 429})

_sleep = time.sleep  # replaced in tests


# ---- service info ---------------------------------------------------------------------------


def _parse_error(resp: httpx.Response) -> tuple[str, str]:
    """The ``{error: {code, message}}`` envelope, or a best effort from the body."""
    try:
        body = resp.json()
    except ValueError:
        text = resp.text.strip()
        return "", text[:300] if text else resp.reason_phrase
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message")
            return (code if isinstance(code, str) else ""), (str(message) if message else "")
        if isinstance(err, str):
            return "", err[:300]
        if body.get("detail"):
            return "", str(body["detail"])[:300]
    return "", str(body)[:300]


def _json_object(resp: httpx.Response, what: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as exc:
        raise StoreError(f"http store: {what} did not return JSON") from exc
    if not isinstance(body, dict):
        raise StoreError(f"http store: {what} must return a JSON object")
    return body


def _capabilities(caps: dict[str, Any]) -> Capabilities:
    metrics = caps.get("metrics")
    if not isinstance(metrics, list) or not metrics or not all(isinstance(m, str) for m in metrics):
        raise StoreError("http store: capabilities.metrics must be a non-empty list of metric names")
    unknown = set(metrics) - set(METRICS)
    if unknown:
        raise StoreError(f"http store: capabilities advertises unknown metrics: {', '.join(sorted(unknown))}")
    max_batch = caps.get("max_batch", DEFAULT_MAX_BATCH)
    if isinstance(max_batch, bool) or not isinstance(max_batch, int) or max_batch < 1:
        raise StoreError("http store: capabilities.max_batch must be a positive integer")
    max_dim = caps.get("max_dim")
    if max_dim is not None and (isinstance(max_dim, bool) or not isinstance(max_dim, int) or max_dim < 1):
        raise StoreError("http store: capabilities.max_dim must be a positive integer or null")
    consistency = caps.get("consistency", "immediate")
    if consistency not in ("immediate", "eventual"):
        raise StoreError("http store: capabilities.consistency must be 'immediate' or 'eventual'")
    filter_exact = caps.get("filter_exact", True)
    if not isinstance(filter_exact, bool):
        raise StoreError("http store: capabilities.filter_exact must be a boolean")
    max_collections = caps.get("max_collections")
    if max_collections is not None and (
        isinstance(max_collections, bool) or not isinstance(max_collections, int) or max_collections < 1
    ):
        raise StoreError("http store: capabilities.max_collections must be a positive integer or null")
    return Capabilities(
        metrics=frozenset(metrics),
        max_batch=max_batch,
        max_dim=max_dim,
        consistency=consistency,
        filter_exact=filter_exact,
        max_collections=max_collections,
    )


def _read_info(client: httpx.Client) -> tuple[str, str, Capabilities]:
    try:
        request = client.build_request("GET", "/info")
        request.headers.pop("Authorization", None)
        resp = client.send(request)
    except httpx.HTTPError as exc:
        raise StoreError(f"http store: could not read /info: {exc}") from exc
    if resp.status_code >= 300:
        _, message = _parse_error(resp)
        raise StoreError(f"http store: /info returned {resp.status_code}: {message or resp.reason_phrase}")
    body = _json_object(resp, "GET /info")
    if body.get("protocol") != PROTOCOL:
        raise StoreError(f"http store: /info reports protocol {body.get('protocol')!r}; expected {PROTOCOL!r}")
    name = body.get("service_name")
    version = body.get("service_version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise StoreError("http store: /info must include string 'service_name' and 'service_version'")
    caps = body.get("capabilities")
    if not isinstance(caps, dict):
        raise StoreError("http store: /info must include a 'capabilities' object")
    return name, version, _capabilities(caps)


# ---- store ----------------------------------------------------------------------------------


class HttpStore:
    protocol = PROTOCOL

    def __init__(
        self, client: httpx.Client, *, service_name: str, service_version: str, capabilities: Capabilities
    ) -> None:
        self._client = client
        self.service_name = service_name
        self.service_version = service_version
        self.capabilities = capabilities

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> HttpStore:
        """Build the store and read its capabilities from ``/info``.

        ``client`` is a test seam: pass a pre-built ``httpx.Client`` (for example one with an ASGI
        transport pointed at an in-process app) and ``url`` is ignored.
        """
        own = client is None
        if client is None:
            client = httpx.Client(
                base_url=url.rstrip("/"), timeout=httpx.Timeout(timeout), headers={"User-Agent": "ragcentre"}
            )
        try:
            # /info is read without credentials (docs/protocols/vector-store-v1.md); the token is added after.
            name, version, capabilities = _read_info(client)
            if own and api_key:
                client.headers["Authorization"] = f"Bearer {api_key}"
        except Exception:
            if own:
                client.close()
            raise
        return cls(client, service_name=name, service_version=version, capabilities=capabilities)

    # ---- request plumbing ---------------------------------------------------------------------

    def _error(self, resp: httpx.Response, what: str) -> Exception:
        code, message = _parse_error(resp)
        text = message or resp.reason_phrase
        if resp.status_code == 404 and code == "collection_not_found":
            return CollectionNotFound(message or f"collection does not exist ({what})")
        if resp.status_code == 409 and code == "collection_mismatch":
            return CollectionMismatch(message or f"collection exists with a different shape ({what})")
        return StoreError(f"http store: {what} returned {resp.status_code}: {text}")

    def _request(self, method: str, path: str, *, retry: bool = False, **kwargs: Any) -> httpx.Response:
        what = f"{method} {path}"
        attempts = MAX_ATTEMPTS if retry else 1
        for attempt in range(attempts):
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                if not retry or attempt == attempts - 1:
                    raise StoreError(f"http store: {what} failed: {exc}") from exc
                _sleep(_backoff(attempt, None))
                continue
            if resp.status_code < 300:
                return resp
            error = self._error(resp, what)
            if retry and _retryable(resp.status_code) and attempt < attempts - 1:
                _sleep(_backoff(attempt, resp))
                continue
            raise error
        raise StoreError(f"http store: {what} failed after {attempts} attempts")  # pragma: no cover

    # ---- VectorStore --------------------------------------------------------------------------

    def info(self, *, fresh: bool = False) -> ServiceInfo:
        if fresh:
            self.service_name, self.service_version, self.capabilities = _read_info(self._client)
        caps = asdict(self.capabilities)
        caps["metrics"] = sorted(self.capabilities.metrics)
        return ServiceInfo(PROTOCOL, self.service_name, self.service_version, capabilities=caps)

    def healthcheck(self) -> None:
        self._request("GET", "/health")

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        body = {
            "dim": spec.dim,
            "metric": spec.metric,
            "fields": {k: v for k, v in spec.fields.items() if k != "document_id"},
        }
        self._request("PUT", f"/collections/{spec.name}", retry=True, json=body)

    def collection_info(self, name: str) -> CollectionInfo | None:
        check_collection_name(name)
        try:
            resp = self._request("GET", f"/collections/{name}")
        except CollectionNotFound:
            return None
        body = _json_object(resp, f"GET /collections/{name}")
        dim, metric, count = body.get("dim"), body.get("metric"), body.get("count")
        if isinstance(dim, bool) or not isinstance(dim, int) or dim < 1:
            raise StoreError(f"http store: GET /collections/{name} returned an invalid dim {dim!r}")
        if metric not in METRICS:
            raise StoreError(f"http store: GET /collections/{name} returned an invalid metric {metric!r}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise StoreError(f"http store: GET /collections/{name} returned an invalid count {count!r}")
        return CollectionInfo(dim=dim, metric=cast(Metric, metric), count=count)

    def drop_collection(self, name: str) -> None:
        check_collection_name(name)
        self._request("DELETE", f"/collections/{name}", retry=True)

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        if not records:
            return
        check_collection_name(name)
        body = {"records": [{"id": r.id, "vector": list(r.vector), "metadata": dict(r.metadata)} for r in records]}
        self._request("POST", f"/collections/{name}/upsert", retry=True, json=body)

    def delete(self, name: str, ids: Sequence[str]) -> None:
        check_collection_name(name)
        if not ids:
            return
        self._request("POST", f"/collections/{name}/delete", retry=True, json={"ids": list(ids)})

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        check_collection_name(name)
        if k < 1:
            return []
        body = {"vector": list(vector), "k": k, "filter": None if filter is None else to_json(filter)}
        resp = self._request("POST", f"/collections/{name}/query", json=body)  # reads are never retried
        data = _json_object(resp, f"POST /collections/{name}/query")
        matches = data.get("matches")
        if not isinstance(matches, list):
            raise StoreError(f"http store: POST /collections/{name}/query must return a 'matches' list")
        out: list[Match] = []
        for item in matches:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise StoreError(f"http store: POST /collections/{name}/query returned a malformed match")
            score = item.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise StoreError(f"http store: POST /collections/{name}/query returned a match without a score")
            out.append(Match(id=item["id"], score=float(score)))
        return out

    def close(self) -> None:
        self._client.close()


def _retryable(status: int) -> bool:
    return status in RETRYABLE_STATUS or status >= 500


def _backoff(attempt: int, resp: httpx.Response | None) -> float:
    delay = min(BASE_BACKOFF * float(2**attempt), MAX_BACKOFF)
    if resp is not None:
        retry_after = parse_retry_after(resp.headers.get("retry-after"))
        if retry_after is not None:
            delay = max(delay, retry_after)
    return delay


# ---- registration ---------------------------------------------------------------------------


class HttpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(description="Base URL of the vector-store service, e.g. http://127.0.0.1:8702")
    timeout: float = Field(default=DEFAULT_TIMEOUT, gt=0, le=3600, description="Per-request timeout in seconds")


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Any) -> HttpStore:
    assert isinstance(config, HttpConfig)
    return HttpStore.connect(url=config.url, timeout=config.timeout, api_key=secrets.get("api_key") or None)


STORE_TYPE = VectorStoreType(
    type="http",
    label="Vector-store service (HTTP)",
    description="Any database behind a service speaking ragcentre.vector_store/v1.",
    config_model=HttpConfig,
    secret_keys=("api_key",),
    # Best case for the registry/UI. A real instance reports its actual capabilities from /info.
    capabilities=Capabilities(metrics=frozenset(METRICS), max_batch=DEFAULT_MAX_BATCH),
    factory=_factory,
    captured_fields=("service_name", "service_version", "capabilities"),
    info_protocol=PROTOCOL,
)
