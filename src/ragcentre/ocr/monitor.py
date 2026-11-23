"""The readiness of every OCR shim RAGCentre knows of or can find.

``snapshot`` never waits for the network. It returns what is known and starts whatever is due on
background threads, so no work happens while nobody is watching:

* every address gets a passive look every ``LOOK_SECONDS``;
* a registered provider gets the OCR probe when it is first seen, comes back, restarts, or is
  edited, again with backoff after a failure, and every ``ocr_recheck_seconds`` after a success
  (an engine can die behind a shim that still answers);
* an address that was only discovered is never sent a document or a credential.

"Ready" is only ever the result of a probe that read the page.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

import httpx

from ragcentre.errors import Conflict, ExecutionError
from ragcentre.logs import redact
from ragcentre.ocr import addresses, discovery, probe
from ragcentre.providers.manager import ProviderRow
from ragcentre.providers.parsers.http import HttpParserConfig
from ragcentre.timeutil import now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext

log = logging.getLogger("ragcentre.ocr")

LOOK_SECONDS = 5.0
MISSES_BEFORE_OFFLINE = 2
SLOW_BEFORE_OFFLINE = 120.0  # a shim can stall /info for about 10 s while it works
RETRY_SECONDS = (30.0, 60.0, 120.0, 300.0, 600.0)
PARALLEL_PROBES = 2  # a probe can load gigabytes of models

OcrState = Literal[
    "looking",
    "detected",
    "checking",
    "ready",
    "failed",
    "engine_down",
    "needs_key",
    "changed",
    "offline",
    "no_ocr",
]
Step = Literal["ok", "failed", "running", "unknown"]

STEPS: dict[OcrState, tuple[Step, Step, Step]] = {
    "looking": ("running", "unknown", "unknown"),
    "detected": ("ok", "unknown", "unknown"),
    "checking": ("ok", "running", "unknown"),
    "ready": ("ok", "ok", "ok"),
    "failed": ("ok", "failed", "unknown"),
    "engine_down": ("ok", "failed", "unknown"),
    "needs_key": ("ok", "unknown", "unknown"),
    "changed": ("ok", "unknown", "unknown"),
    "offline": ("failed", "unknown", "unknown"),
    "no_ocr": ("ok", "unknown", "unknown"),
}

Spawn = Callable[[Callable[[], None]], None]


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="ocr-status", daemon=True).start()


@dataclass
class Watched:
    """What is known about one address."""

    url: str
    revision: str = ""  # the provider's updated_at
    info: discovery.ShimInfo | None = None
    last: discovery.LookKind | None = None
    online: bool = False
    looking: bool = False
    next_look: float = 0.0
    misses: int = 0
    slow_since: float | None = None
    generation: int = 0  # raised whenever earlier evidence stops counting
    probing: bool = False
    manual: bool = False
    result: probe.ProbeResult | None = None
    checked_at: str | None = None
    next_probe: float | None = 0.0  # None: not until something changes
    failures: int = 0

    def forget_result(self) -> None:
        self.generation += 1
        self.probing = self.manual = False
        self.result = self.checked_at = None
        self.next_probe, self.failures = 0.0, 0


@dataclass(frozen=True)
class Target:
    """A registered HTTP parser, read from the database on the caller's thread."""

    row: ProviderRow
    config: HttpParserConfig
    api_key: str | None
    key_error: str | None

    @property
    def url(self) -> str:
        return self.config.base_url.rstrip("/")


class OcrMonitor:
    def __init__(
        self,
        ctx: AppContext,
        *,
        clock: Callable[[], float] = time.monotonic,
        spawn: Spawn = _thread,
        container: bool | None = None,
    ) -> None:
        self.ctx = ctx
        self._clock = clock
        self._spawn = spawn
        self.container = addresses.in_container() if container is None else container
        self.candidates = addresses.candidate_urls(ctx.settings.discovery_urls, container=self.container)
        self._lock = threading.RLock()
        self._probe_slots = threading.BoundedSemaphore(PARALLEL_PROBES)
        self._providers: dict[str, Watched] = {}
        self._found = {url: Watched(url) for url in self.candidates}
        self._closed = False
        # Discovered addresses must not go through an environment proxy. Registered providers are
        # reached exactly as ingestion reaches them.
        self._discovery_http: httpx.Client = ctx.http_client_factory(trust_env=False)
        self._provider_http: httpx.Client = ctx.http_client_factory()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._discovery_http.close()
        self._provider_http.close()

    # ---- the public calls -------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        targets, all_names = self._targets()
        work: list[Callable[[], None]] = []
        with self._lock:
            self._sync(targets)
            if not self._closed:
                work = self._due(targets)
        for job in work:
            self._spawn(job)
        with self._lock:
            return self._describe(targets, all_names)

    def scan(self) -> dict[str, Any]:
        """Look at every address now."""
        with self._lock:
            for watched in [*self._providers.values(), *self._found.values()]:
                watched.next_look = 0.0
        return self.snapshot()

    def check(self, provider_id: str) -> dict[str, Any]:
        """Run the OCR probe now, or as soon as the shim answers."""
        row = self.ctx.providers.get(provider_id)
        if (row.kind, row.type) != ("parser", "http"):
            raise Conflict(f"{row.name} is not an HTTP parser, so it has no OCR engine to check.")
        targets, _ = self._targets()
        with self._lock:
            self._sync(targets)
            watched = self._providers.get(provider_id)
            if watched is not None:
                watched.next_look = 0.0
                if not watched.probing:
                    watched.next_probe, watched.manual = 0.0, True
        return self.snapshot()

    # ---- what to do ---------------------------------------------------------------------------

    def _targets(self) -> tuple[list[Target], set[str]]:
        rows = self.ctx.providers.list()
        fields = set(HttpParserConfig.model_fields)
        targets = []
        for row in rows:
            if (row.kind, row.type) != ("parser", "http"):
                continue
            config = HttpParserConfig.model_validate({k: v for k, v in row.config.items() if k in fields})
            key, error = None, None
            try:
                key = self.ctx.providers.secret_values(row.id).get("api_key")
            except ExecutionError as exc:
                error = redact(exc.message)
            targets.append(Target(row, config, key, error))
        return targets, {row.name for row in rows}

    def _sync(self, targets: list[Target]) -> None:
        wanted = {t.row.id for t in targets}
        for gone in set(self._providers) - wanted:
            del self._providers[gone]
        for target in targets:
            watched = self._providers.get(target.row.id)
            if watched is None or watched.url != target.url:
                self._providers[target.row.id] = Watched(target.url, revision=target.row.updated_at)
            elif watched.revision != target.row.updated_at:
                watched.revision, watched.next_look = target.row.updated_at, 0.0
                watched.forget_result()

    def _due(self, targets: list[Target]) -> list[Callable[[], None]]:
        now = self._clock()
        work: list[Callable[[], None]] = []
        for target in targets:
            watched = self._providers[target.row.id]
            if not watched.looking and now >= watched.next_look:
                watched.looking = True
                work.append(self._looker(watched, self._provider_http))
            if self._should_probe(target, watched, now):
                watched.probing = True
                work.append(self._prober(target, watched))
        for watched in self._found.values():
            if not watched.looking and now >= watched.next_look:
                watched.looking = True
                work.append(self._looker(watched, self._discovery_http))
        return work

    def _should_probe(self, target: Target, watched: Watched, now: float) -> bool:
        if watched.probing or watched.next_probe is None or now < watched.next_probe:
            return False
        if not watched.online or watched.info is None or watched.last != "up" or target.key_error:
            return False
        if watched.info.engine == "unreachable" and not watched.manual:
            return False
        return self._drift(target, watched.info) is None

    @staticmethod
    def _drift(target: Target, info: discovery.ShimInfo) -> str | None:
        pinned_name = target.row.config.get("service_name")
        pinned_version = target.row.config.get("service_version")
        if pinned_name and info.name != pinned_name:
            return f"{info.name} answers at this address; this provider was created for {pinned_name}."
        if target.config.on_version_drift == "fail" and info.version != pinned_version:
            return f"The shim now reports {info.version}; this provider is pinned to {pinned_version}."
        return None

    # ---- background work ----------------------------------------------------------------------

    def _looker(self, watched: Watched, client: httpx.Client) -> Callable[[], None]:
        def work() -> None:
            try:
                seen = discovery.look(client, watched.url)
            except Exception:  # never leave an address marked as being looked at
                log.debug("could not look at a shim address", exc_info=True)
                seen = discovery.Look("foreign")
            with self._lock:
                watched.looking = False
                watched.next_look = self._clock() + LOOK_SECONDS
                self._apply(watched, seen)

        return work

    def _apply(self, watched: Watched, seen: discovery.Look) -> None:
        now = self._clock()
        before = watched.info
        watched.last = seen.kind
        if seen.kind == "slow":
            watched.misses = 0
            if watched.slow_since is None:
                watched.slow_since = now
            if watched.online and now - watched.slow_since >= SLOW_BEFORE_OFFLINE:
                watched.online = False
                watched.forget_result()
            return
        watched.slow_since = None
        if seen.kind == "up" and seen.info is not None:
            watched.misses = 0
            if not watched.online or before is None or self._replaced(before, seen.info):
                watched.forget_result()
            elif seen.info.engine != before.engine:
                if seen.info.engine == "error" and not watched.probing:
                    watched.next_probe = now  # a real document just hit an engine failure
                elif before.engine == "unreachable":
                    watched.forget_result()  # the engine is back: prove it
            watched.online, watched.info = True, seen.info
        elif seen.kind in ("gone", "failing"):
            watched.misses += 1
            if watched.online and watched.misses >= MISSES_BEFORE_OFFLINE:
                watched.online = False
                watched.forget_result()
        else:  # denied, or something that is not a shim
            watched.online, watched.info = False, None
            watched.forget_result()

    @staticmethod
    def _replaced(before: discovery.ShimInfo, after: discovery.ShimInfo) -> bool:
        return (before.name, before.version, before.instance_id) != (after.name, after.version, after.instance_id)

    def _prober(self, target: Target, watched: Watched) -> Callable[[], None]:
        generation = watched.generation
        mime_types = watched.info.mime_types if watched.info else ()

        def work() -> None:
            result = probe.ProbeResult("error", detail="The check did not finish.")
            try:
                with self._probe_slots:
                    if self._closed or watched.generation != generation:
                        return
                    started = time.monotonic()
                    found = probe.run_probe(target.config, target.api_key, mime_types, self.ctx.http_client_factory)
                    result = replace(found, seconds=round(time.monotonic() - started, 2))
            finally:
                with self._lock:
                    if watched.generation == generation:
                        self._record(watched, result)

        return work

    def _record(self, watched: Watched, result: probe.ProbeResult) -> None:
        now = self._clock()
        watched.probing = watched.manual = False
        watched.result, watched.checked_at = result, now_iso()
        watched.next_look = 0.0
        if result.ok:
            watched.failures = 0
            every = self.ctx.settings.ocr_recheck_seconds
            watched.next_probe = now + every if every > 0 else None
            if watched.info and watched.info.engine in ("error", "idle", "loading"):
                watched.info = replace(watched.info, engine="ready")  # newer than the last look
        elif result.code in ("needs_key", "no_ocr"):
            watched.next_probe = None  # waiting would not change the answer
        else:
            watched.next_probe = now + RETRY_SECONDS[min(watched.failures, len(RETRY_SECONDS) - 1)]
            watched.failures += 1

    # ---- what to show -------------------------------------------------------------------------

    def _state(self, target: Target, watched: Watched) -> tuple[OcrState, str, str | None]:
        """``(state, what it means and what to do, technical detail)``."""
        if target.key_error:
            return "needs_key", "The provider's API key cannot be read. Fix the secret.", target.key_error
        if not watched.online:
            return self._not_online(watched)
        info, result = watched.info, watched.result
        assert info is not None
        detail = result.detail if result else None
        if info.engine == "unreachable":
            return (
                "engine_down",
                "The shim is running, but it cannot reach its OCR engine. Start the engine or check its URL; "
                "RAGCentre tests it again when it returns.",
                detail,
            )
        if drift := self._drift(target, info):
            return "changed", "Create a new provider from this one to use the shim that runs now.", drift
        if watched.probing and watched.manual:
            return "checking", "Reading a scanned test page.", None
        if info.engine == "error" and result is not None and result.ok:
            return (
                "engine_down",
                "The shim is running, but its OCR engine failed on the last document. Check the shim's log.",
                detail,
            )
        if result is None:
            return "checking", "Reading a scanned test page. A first run loads models and can take minutes.", None
        match result.code:
            case "read":
                return "ready", "The OCR engine read a scanned test page.", None
            case "no_ocr":
                return "no_ocr", "This parser accepts neither scanned PDFs nor PNG images.", None
            case "needs_key":
                return "needs_key", "The shim rejected the test page. Set its API key on this provider.", detail
            case "engine_unavailable":
                return (
                    "engine_down",
                    "The shim is running, but its OCR engine is not. Check the shim's log, or the engine's URL "
                    "if it runs elsewhere.",
                    detail,
                )
            case "no_text":
                return (
                    "failed",
                    "The shim answered, but returned none of the scanned page's text. Check that OCR is on and "
                    "that the engine's models are installed.",
                    detail,
                )
            case "rejected":
                return (
                    "failed",
                    "The shim could not OCR a scanned page. The engine's models may be missing; check the shim's log.",
                    detail,
                )
            case "unreachable":
                return "failed", "The connection was lost during the test. RAGCentre will try again.", detail
            case "busy":
                return "failed", "The shim was too busy to take the test. RAGCentre will try again.", detail
            case "error":
                return "failed", "The OCR test did not finish correctly. RAGCentre will try again.", detail

    def _not_online(self, watched: Watched) -> tuple[OcrState, str, str | None]:
        if watched.last is None:
            return "looking", "Looking for the shim.", None
        if watched.last == "denied":
            return "needs_key", "The shim refused GET /info, which the parser protocol requires to be open.", None
        if watched.last == "foreign":
            return "offline", "Something answers at this address, but it is not a RAGCentre parser shim.", None
        if watched.last == "slow" and watched.info is None:
            since = watched.slow_since
            if since is not None and self._clock() - since < SLOW_BEFORE_OFFLINE:
                return "looking", "The address accepts connections. Waiting for the shim to answer.", None
        where = addresses.split_url(watched.url)
        if self.container and where and where[0] in addresses.LOOPBACK:
            hint = f"http://{addresses.DOCKER_HOST}:{where[1]}"
            message = (
                f"RAGCentre runs in a container, where {where[0]} is the container itself. "
                f"For a shim on this machine use {hint}."
            )
            return "offline", message, None
        return "offline", "The shim is not answering. Start it; RAGCentre tests it as soon as it is back.", None

    def _describe(self, targets: list[Target], all_names: set[str]) -> dict[str, Any]:
        services: list[dict[str, Any]] = []
        for target in targets:
            watched = self._providers.get(target.row.id)
            if watched is None:  # deleted by a concurrent request
                continue
            state, message, detail = self._state(target, watched)
            entry = self._entry(watched, state, message, detail)
            entry.update(
                id=target.row.id, provider_id=target.row.id, provider_name=target.row.name, suggested_name=None
            )
            services.append(entry)
        shown: list[Watched] = []
        for watched in self._found.values():
            if not watched.online or watched.info is None:
                continue
            if any(self._same(watched, p) for p in self._providers.values()):
                continue
            if any(self._same(watched, s) for s in shown):
                continue
            shown.append(watched)
            services.append(self._found_entry(watched, all_names))
        everyone = [*self._providers.values(), *self._found.values()]
        return {
            "services": services,
            "scanning": any(w.last is None for w in everyone),
            "discovery_enabled": bool(self.candidates),
        }

    @staticmethod
    def _same(found: Watched, known: Watched) -> bool:
        if found.url == known.url:
            return True
        a, b = found.info, known.info
        if a and b and a.instance_id and b.instance_id:
            return a.instance_id == b.instance_id
        if not addresses.same_service_address(found.url, known.url):
            return False
        if known.last is None:
            return True  # not looked at yet: don't flash it as a new find
        return bool(known.online and a and b and (a.name, a.version) == (b.name, b.version))

    def _found_entry(self, watched: Watched, taken: set[str]) -> dict[str, Any]:
        assert watched.info is not None
        if watched.info.engine == "unreachable":
            state: OcrState = "engine_down"
            message = "Found a shim, but it cannot reach its OCR engine. Start the engine before adding it."
        elif not probe.probe_formats(watched.info.mime_types):
            state, message = "no_ocr", "Found a parser shim that accepts neither scanned PDFs nor images."
        else:
            state, message = "detected", "Found a shim. Add it to test whether its OCR engine works."
        entry = self._entry(watched, state, message, None)
        entry.update(
            id="found-" + hashlib.sha256(watched.url.encode()).hexdigest()[:16],
            provider_id=None,
            provider_name=None,
            suggested_name=suggest_name(watched.info.name, taken),
        )
        return entry

    @staticmethod
    def _entry(watched: Watched, state: OcrState, message: str, detail: str | None) -> dict[str, Any]:
        shim, engine, text = STEPS[state]
        if state == "failed" and watched.result:
            if watched.result.code == "no_text":
                engine, text = "unknown", "failed"  # it answered, so only the missing text is proven
            elif watched.result.code in ("unreachable", "busy", "error"):
                engine = "unknown"
        return {
            "base_url": watched.url,
            "name": watched.info.name if watched.info else None,
            "version": watched.info.version if watched.info else None,
            "state": state,
            "steps": {"shim": shim, "engine": engine, "text": text},
            "busy": watched.online and watched.last == "slow",
            "rechecking": watched.probing and state != "checking",
            "message": message,
            "detail": detail,
            "checked_at": watched.checked_at,
            "check_seconds": watched.result.seconds if watched.result else None,
        }


def suggest_name(service: str, taken: set[str]) -> str:
    """``docling-shim`` -> ``docling``, or ``docling-2`` when that is taken."""
    base = re.sub(r"[^a-z0-9]+", "-", service.lower()).strip("-").removesuffix("-shim")[:50].rstrip("-")
    if not base or not base[0].isalpha():
        base = "ocr"
    name, number = base, 2
    while name in taken:
        name, number = f"{base}-{number}", number + 1
    return name
