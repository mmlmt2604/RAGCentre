"""Talk to a ``ragcentre.parser/v1`` shim at a URL: ``GET /info`` and ``POST /parse``, sync or async.

The runner never starts an engine. The URL can be a local process, a container or another machine.
Set ``OCR_LIVE_API_KEY_<NAME>`` (the engine name in upper case) to send ``Authorization: Bearer``.
"""

from __future__ import annotations

import json
import mimetypes
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

MIME_OVERRIDES = {
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
POLL_MIN, POLL_MAX = 0.1, 30.0


@dataclass(frozen=True)
class Reply:
    """One answer. ``body`` is the decoded JSON (or the text when the body is not JSON).

    ``seconds`` is wall time for the whole call, polling included. ``mode`` is ``sync`` or ``async``."""

    status: int
    body: Any
    seconds: float
    mode: str = "sync"

    @property
    def error_code(self) -> str | None:
        error = self.body.get("error") if isinstance(self.body, dict) else None
        return error.get("code") if isinstance(error, dict) else None


def _decode(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def mime_type(path: Path) -> str:
    return MIME_OVERRIDES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


class EngineClient:
    """``timeout`` limits each request. ``job_timeout`` limits an async parse from submit to result."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 600.0,
        job_timeout: float = 3600.0,
        http: httpx.Client | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = http or httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)
        self.job_timeout = job_timeout

    def close(self) -> None:
        self.http.close()

    def info(self) -> Reply:
        start = time.perf_counter()
        response = self.http.get("/info")
        return Reply(response.status_code, _decode(response), time.perf_counter() - start)

    def parse(self, path: Path, options: Mapping[str, Any] | None = None, *, use_async: bool | None = None) -> Reply:
        """Send ``path`` with ``options``. ``use_async`` ``None`` follows ``/info.capabilities.async``."""
        if use_async is None:
            info = self.info()
            use_async = bool(
                info.status == 200
                and isinstance(info.body, dict)
                and (info.body.get("capabilities") or {}).get("async")
            )
        start = time.perf_counter()
        files = {"file": (path.name, path.read_bytes(), mime_type(path))}
        data = {"options": json.dumps(dict(options or {}))}
        response = self.http.post("/parse", params={"mode": "async"} if use_async else None, files=files, data=data)
        if response.status_code == 202 and isinstance(_decode(response), dict) and "job_id" in _decode(response):
            return self._finish_job(_decode(response), start)
        return Reply(response.status_code, _decode(response), time.perf_counter() - start, "sync")

    def _finish_job(self, accepted: dict[str, Any], start: float) -> Reply:
        job_id = accepted["job_id"]
        delay = min(max(float(accepted.get("poll_after_seconds", 2)), POLL_MIN), POLL_MAX)
        deadline = start + self.job_timeout
        while True:
            time.sleep(delay)
            status = self.http.get(f"/jobs/{job_id}")
            body = _decode(status)
            if status.status_code != 200:
                return Reply(status.status_code, body, time.perf_counter() - start, "async")
            if body.get("status") != "running":
                break
            if time.perf_counter() > deadline:
                return Reply(
                    504,
                    {"error": {"code": "job_timeout", "message": f"job {job_id} still running"}},
                    time.perf_counter() - start,
                    "async",
                )
            delay = min(delay * 1.5, POLL_MAX)
        result = self.http.get(f"/jobs/{job_id}/result")
        reply = Reply(result.status_code, _decode(result), time.perf_counter() - start, "async")
        self.http.delete(f"/jobs/{job_id}")  # best effort, as the host does
        return reply
