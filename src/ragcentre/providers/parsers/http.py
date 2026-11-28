"""HTTP parser client for services speaking ``ragcentre.parser/v1`` (section 8.1)."""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol

import httpx
from pydantic import Field

from ragcentre.errors import ErrorCode, ExecutionError
from ragcentre.providers.base import (
    RETRYABLE_STATUS,
    BuildEnv,
    HttpConfig,
    InfoMixin,
    ProviderTypeSpec,
    invalid_response,
)

PROTOCOL = "ragcentre.parser/v1"

# Polling interval bounds and growth. The service's ``poll_after_seconds`` is the first interval.
MIN_POLL_SECONDS = 0.5
MAX_POLL_SECONDS = 30.0
POLL_BACKOFF = 1.5
# Consecutive failed status requests tolerated while a job runs (a service restarting, a network blip).
MAX_POLL_ERRORS = 5

_sleep: Callable[[float], None] = time.sleep  # replaced in tests


class JobStore(Protocol):
    """Where an in-flight async job id is remembered, so a restarted worker resumes polling."""

    def get(self) -> str | None: ...

    def save(self, job_id: str) -> None: ...

    def clear(self) -> None: ...


class HttpParserConfig(HttpConfig):
    timeout_seconds: float = Field(
        default=600,
        gt=0,
        le=3600,
        description="Timeout of each request (the whole parse when the service is sync-only)",
    )
    job_timeout_seconds: float = Field(
        default=21600,
        gt=0,
        le=604800,
        description="How long to wait for one document when the service parses asynchronously (default 6 hours)",
    )
    revision: str = Field(default="", description="Optional revision tag; set at creation; a new value is a new fingerprint")
    on_version_drift: Literal["fail", "warn"] = Field(
        default="fail", description="What to do when the service reports a different version"
    )


class HttpParser(InfoMixin):
    protocol = PROTOCOL
    # The slowest job polling. The OCR check lowers it: someone is watching that one.
    max_poll_seconds = MAX_POLL_SECONDS

    def __init__(self, config: HttpParserConfig, secrets: Mapping[str, str], env: BuildEnv) -> None:
        super().__init__(config, secrets, env)
        self._job_timeout = config.job_timeout_seconds

    def parse(
        self,
        content: bytes,
        filename: str,
        mime_type: str,
        options: Mapping[str, Any],
        job: JobStore | None = None,
    ) -> Any:
        """Parse one document. A service that reports ``capabilities.async`` is used through a job,
        so a document can take longer than ``timeout_seconds``; with ``job`` given, the job id is
        remembered while it runs."""
        if self.info().capabilities.get("async") is True:
            return self._parse_async(content, filename, mime_type, options, job)
        return self._check(self.json("POST", "/parse", **self._form(content, filename, mime_type, options)))

    def _form(self, content: bytes, filename: str, mime_type: str, options: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "files": {"file": (filename or "document", content, mime_type)},
            "data": {"options": json.dumps(dict(options))},
        }

    def _check(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise invalid_response(self.name, "/parse must return a Document IR object")
        return body

    # ---- async mode (docs/protocols/parser-v1.md, "Async jobs") -------------------------------

    def _parse_async(
        self, content: bytes, filename: str, mime_type: str, options: Mapping[str, Any], store: JobStore | None
    ) -> dict[str, Any]:
        job_id = store.get() if store is not None else None
        resumed = job_id is not None
        if job_id is None:
            form = self._form(content, filename, mime_type, options)
            body = self.json("POST", "/parse", params={"mode": "async"}, **form)
            job_id = body.get("job_id") if isinstance(body, dict) else None
            if not isinstance(job_id, str) or not job_id:
                # A service that ignores ?mode=async answers with the IR itself.
                if isinstance(body, dict) and "blocks" in body:
                    return body
                raise invalid_response(self.name, "/parse?mode=async must return a job_id")
            if store is not None:
                store.save(job_id)
            interval = min(_poll_interval(body.get("poll_after_seconds")), self.max_poll_seconds)
        else:
            interval = MIN_POLL_SECONDS
        try:
            result = self._wait(job_id, interval)
        except _JobLost:
            if store is not None:
                store.clear()
            if resumed:  # the service restarted or expired the job while we were away: submit again
                return self._parse_async(content, filename, mime_type, options, store)
            raise ExecutionError(
                ErrorCode.PROVIDER_ERROR.value,
                f"{self.name}: job {job_id} vanished right after it was submitted",
                retryable=True,
                provider=self.name,
            ) from None
        except _JobFailed as failed:
            if store is not None:
                store.clear()  # the job is finished for good; a retry must submit a fresh one
            raise failed.error from None
        if store is not None:
            store.clear()
        self._forget(job_id)
        return result

    def _wait(self, job_id: str, interval: float) -> dict[str, Any]:
        deadline = time.monotonic() + self._job_timeout
        errors = 0
        while True:
            _sleep(interval)
            if time.monotonic() > deadline:
                raise ExecutionError(
                    ErrorCode.PROVIDER_ERROR.value,
                    f"{self.name}: job {job_id} did not finish within {self._job_timeout:g} s",
                    retryable=True,
                    provider=self.name,
                )
            try:
                resp = self.client.get(f"{self.base_url}/jobs/{job_id}")
            except httpx.HTTPError:
                resp = None
            if resp is None or resp.status_code >= 500 or resp.status_code in RETRYABLE_STATUS:
                errors += 1
                if errors >= MAX_POLL_ERRORS:
                    raise ExecutionError(
                        ErrorCode.PROVIDER_ERROR.value,
                        f"{self.name}: could not read the status of job {job_id}",
                        retryable=True,
                        provider=self.name,
                    )
                interval = min(interval * POLL_BACKOFF, self.max_poll_seconds)
                continue
            errors = 0
            if resp.status_code == 404:
                raise _JobLost
            if resp.status_code != 200:
                self.request("GET", f"/jobs/{job_id}")  # raises the classified error
            status = _json_object(resp, self.name)
            state = status.get("status")
            if state == "running":
                interval = min(interval * POLL_BACKOFF, self.max_poll_seconds)
                continue
            if state == "succeeded":
                return self._check(self.json("GET", f"/jobs/{job_id}/result"))
            if state == "failed":
                raise _JobFailed(self._job_error(status.get("error")))
            raise invalid_response(self.name, f"job status must be running, succeeded or failed, got {state!r}")

    def _job_error(self, error: Any) -> ExecutionError:
        info = error if isinstance(error, dict) else {}
        status = info.get("http_status")
        status = status if isinstance(status, int) else 422
        return ExecutionError(
            ErrorCode.PROVIDER_ERROR.value,
            f"{self.name}: upstream parse job failed (HTTP {status})",
            retryable=status in RETRYABLE_STATUS or status >= 500,
            provider=self.name,
            upstream_status=status,
        )

    def _forget(self, job_id: str) -> None:
        # Best effort: frees the service's memory before its TTL does.
        with contextlib.suppress(httpx.HTTPError):
            self.client.delete(f"{self.base_url}/jobs/{job_id}")


class _JobLost(Exception):
    """The service no longer knows the job."""


class _JobFailed(Exception):
    """The job ended in ``failed``; ``error`` is what to raise."""

    def __init__(self, error: ExecutionError) -> None:
        super().__init__(str(error))
        self.error = error


def _poll_interval(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return MIN_POLL_SECONDS * 4
    return min(max(float(value), MIN_POLL_SECONDS), MAX_POLL_SECONDS)


def _json_object(resp: httpx.Response, provider: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise invalid_response(provider, "/jobs/{id} did not return a JSON object")
    return body


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> HttpParser:
    assert isinstance(config, HttpParserConfig)
    return HttpParser(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="parser",
    type="http",
    label="Parser service (HTTP)",
    description="Your own parser or OCR service speaking ragcentre.parser/v1.",
    config_model=HttpParserConfig,
    fingerprint_fields=("service_name", "service_version", "revision"),
    secret_keys=("api_key",),
    factory=_factory,
    captured_fields=("service_name", "service_version", "options_schema"),
    info_protocol=PROTOCOL,
)
