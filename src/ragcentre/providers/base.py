"""Provider base classes, the shared HTTP helper and error classification (section 8).

Error classification is implemented once, here: connection errors, timeouts, HTTP 408, 429 and
5xx are retryable; other 4xx are permanent; a response that fails validation is permanent. A
``Retry-After`` header on 429/503 sets the minimum retry delay.
"""

from __future__ import annotations

import builtins
import email.utils
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ragcentre.errors import ErrorCode, ExecutionError
from ragcentre.logs import redact

if TYPE_CHECKING:
    from ragcentre.stores.base import Capabilities

ProviderKind = Literal["parser", "chunker", "embedder", "reranker", "vector_store"]
KINDS: tuple[ProviderKind, ...] = ("parser", "chunker", "embedder", "reranker", "vector_store")
Role = Literal["document", "query"]

RETRYABLE_STATUS = frozenset({408, 429})


# ---- configuration models --------------------------------------------------------------------


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HttpConfig(ProviderConfig):
    base_url: str = Field(description="Service URL", json_schema_extra={"format": "uri"})
    timeout_seconds: float = Field(default=60, gt=0, le=3600, description="Request timeout in seconds")
    max_concurrency: int = Field(default=4, ge=1, le=256, description="Parallel requests")


# ---- service info (parsers and custom chunkers) ----------------------------------------------


@dataclass(frozen=True)
class ServiceInfo:
    protocol: str
    name: str
    version: str
    mime_types: list[str] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)


# ---- provider interfaces ---------------------------------------------------------------------


@runtime_checkable
class Parser(Protocol):
    def parse(self, content: bytes, filename: str, mime_type: str, options: Mapping[str, Any]) -> Any: ...


@runtime_checkable
class Chunker(Protocol):
    def chunk(self, document: Mapping[str, Any], options: Mapping[str, Any]) -> list[dict[str, Any]]: ...


@runtime_checkable
class Embedder(Protocol):
    batch_size: int

    def embed(self, texts: Sequence[str], role: Role) -> list[list[float]]: ...


@runtime_checkable
class Reranker(Protocol):
    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[tuple[int, float]]: ...


@runtime_checkable
class InfoService(Protocol):
    def info(self) -> ServiceInfo: ...


# ---- type registration -----------------------------------------------------------------------


@dataclass(frozen=True)
class BuildEnv:
    """What a factory receives besides its config and secrets."""

    provider_id: str
    provider_name: str
    http: Callable[..., httpx.Client]
    data_dir: Any  # pathlib.Path
    semaphore: threading.BoundedSemaphore


Factory = Callable[[BaseModel, dict[str, str], BuildEnv], Any]


@dataclass(frozen=True)
class ProviderTypeSpec:
    kind: ProviderKind
    type: str
    label: str
    description: str
    config_model: builtins.type[ProviderConfig] | builtins.type[BaseModel]
    fingerprint_fields: tuple[str, ...]
    secret_keys: tuple[str, ...]
    factory: Factory
    # Values the code sets on creation (e.g. impl_version); the user cannot set or change them.
    fixed_config: Mapping[str, Any] = field(default_factory=dict)
    # Fields captured from the service's /info at creation (HTTP parsers and chunkers).
    captured_fields: tuple[str, ...] = ()
    info_protocol: str | None = None
    capabilities: Capabilities | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.type)

    def operational_fields(self) -> set[str]:
        return set(self.config_model.model_fields) - set(self.fingerprint_fields)


# ---- HTTP helper -----------------------------------------------------------------------------


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        text = resp.text.strip()
        return text[:300] if text else resp.reason_phrase
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        if isinstance(err, str):
            return err[:300]
        if body.get("detail"):
            return str(body["detail"])[:300]
    return str(body)[:300]


def classify_response(resp: httpx.Response, provider: str) -> ExecutionError | None:
    """``None`` for 2xx; otherwise the classified error."""
    status = resp.status_code
    if 200 <= status < 300:
        return None
    retryable = status in RETRYABLE_STATUS or status >= 500
    retry_after = parse_retry_after(resp.headers.get("retry-after")) if status in (429, 503) else None
    where = f"{resp.request.method} {resp.request.url}" if resp.request is not None else ""
    message = f"{provider}: {where} returned {status} {resp.reason_phrase}: {_error_message(resp)}"
    return ExecutionError(
        ErrorCode.PROVIDER_ERROR.value,
        redact(message),
        retryable=retryable,
        retry_after=retry_after,
        provider=provider,
    )


def classify_exception(exc: Exception, provider: str, url: str) -> ExecutionError:
    if isinstance(exc, httpx.TimeoutException):
        message = f"{provider}: request to {url} timed out"
    elif isinstance(exc, httpx.TransportError):
        message = f"{provider}: could not connect to {url}: {exc}"
    else:  # pragma: no cover - defensive
        message = f"{provider}: request to {url} failed: {exc}"
    return ExecutionError(ErrorCode.PROVIDER_ERROR.value, redact(message), retryable=True, provider=provider)


def invalid_response(provider: str, detail: str) -> ExecutionError:
    return ExecutionError(
        ErrorCode.PROVIDER_ERROR.value,
        redact(f"{provider}: invalid response: {detail}"),
        retryable=False,
        provider=provider,
    )


class HttpService:
    """Shared plumbing for HTTP providers: one client, auth header, concurrency limit."""

    def __init__(self, config: HttpConfig, secrets: Mapping[str, str], env: BuildEnv) -> None:
        self.name = env.provider_name
        self.base_url = config.base_url.rstrip("/")
        headers = {"User-Agent": "ragcentre"}
        api_key = secrets.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._semaphore = env.semaphore
        self.client = env.http(timeout=httpx.Timeout(config.timeout_seconds), headers=headers)
        self._info: tuple[float, ServiceInfo] | None = None
        self._info_lock = threading.Lock()

    @contextmanager
    def _slot(self) -> Iterator[None]:
        with self._semaphore:
            yield

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{path}"
        with self._slot():
            try:
                resp = self.client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise classify_exception(exc, self.name, url) from exc
        error = classify_response(resp, self.name)
        if error is not None:
            raise error
        return resp

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self.request(method, path, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise invalid_response(self.name, f"{path} did not return JSON") from exc

    def close(self) -> None:
        self.client.close()


class InfoMixin(HttpService):
    """``GET /info`` for parser and chunker services, cached for five minutes."""

    INFO_TTL = 300.0
    protocol = ""

    def info(self, *, fresh: bool = False) -> ServiceInfo:
        with self._info_lock:
            cached = self._info
        if cached is not None and not fresh and time.monotonic() - cached[0] < self.INFO_TTL:
            return cached[1]
        body = self.json("GET", "/info")
        if not isinstance(body, dict):
            raise invalid_response(self.name, "/info must return an object")
        protocol = body.get("protocol")
        if protocol != self.protocol:
            raise invalid_response(self.name, f"/info reports protocol {protocol!r}; expected {self.protocol!r}")
        name, version = body.get("name"), body.get("version")
        if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
            raise invalid_response(self.name, "/info must include string 'name' and 'version'")
        mime_types = body.get("mime_types") or []
        caps = body.get("capabilities") or {}
        info = ServiceInfo(
            protocol=protocol,
            name=name,
            version=version,
            mime_types=[str(m) for m in mime_types] if isinstance(mime_types, list) else [],
            capabilities=caps if isinstance(caps, dict) else {},
        )
        with self._info_lock:
            self._info = (time.monotonic(), info)
        return info
