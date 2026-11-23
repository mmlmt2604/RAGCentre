"""One passive look at one address: ``GET /info``, bounded in time and size."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from ragcentre.providers.parsers.http import PROTOCOL

TIMEOUT = httpx.Timeout(3.0, connect=2.0)
TOTAL_SECONDS = 8.0  # a body that trickles in
MAX_BODY = 256 * 1024
MAX_TEXT = 200

EngineReport = Literal["idle", "loading", "ready", "error", "unreachable"]
REPORTS: tuple[EngineReport, ...] = ("idle", "loading", "ready", "error", "unreachable")
# up: a shim answered. gone: nothing listens. slow: it accepted the connection and did not answer
# (a busy engine can hold the process for seconds). denied: 401/403. failing: 5xx.
# foreign: something else lives there.
LookKind = Literal["up", "gone", "slow", "denied", "failing", "foreign"]


@dataclass(frozen=True)
class ShimInfo:
    name: str
    version: str
    mime_types: tuple[str, ...] = ()
    engine: EngineReport | None = None
    instance_id: str | None = None


@dataclass(frozen=True)
class Look:
    kind: LookKind
    info: ShimInfo | None = None


def read_info(body: bytes) -> ShimInfo | None:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("protocol") != PROTOCOL:
        return None
    name, version = data.get("name"), data.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        return None
    if not 0 < len(name) <= MAX_TEXT or not 0 < len(version) <= MAX_TEXT:
        return None
    mimes = data.get("mime_types")
    raw = data.get("engine")
    engine = raw if isinstance(raw, dict) else {}
    instance = engine.get("instance_id")
    return ShimInfo(
        name=name,
        version=version,
        mime_types=tuple(m for m in mimes if isinstance(m, str))[:100] if isinstance(mimes, list) else (),
        engine=next((report for report in REPORTS if report == engine.get("state")), None),
        instance_id=instance if isinstance(instance, str) and 0 < len(instance) <= MAX_TEXT else None,
    )


def look(client: httpx.Client, url: str) -> Look:
    """Never sends credentials: the protocol keeps ``/info`` open, and this runs every few seconds
    against whatever answers at the address."""
    deadline = time.monotonic() + TOTAL_SECONDS
    try:
        with client.stream("GET", f"{url}/info", timeout=TIMEOUT, follow_redirects=False) as resp:
            if resp.status_code in (401, 403):
                return Look("denied")
            if resp.status_code >= 500:
                return Look("failing")
            if resp.status_code != 200:
                return Look("foreign")
            body = bytearray()
            for part in resp.iter_bytes():
                body += part
                if len(body) > MAX_BODY:
                    return Look("foreign")
                if time.monotonic() > deadline:
                    return Look("slow")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return Look("gone")
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return Look("slow" if isinstance(exc, httpx.TransportError) else "foreign")
    info = read_info(bytes(body))
    return Look("up", info) if info else Look("foreign")
