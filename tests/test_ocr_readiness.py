"""OCR shim discovery and readiness (``ragcentre.ocr``)."""

from __future__ import annotations

import re
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from fastapi.testclient import TestClient

from ragcentre.api.app import create_app
from ragcentre.context import AppContext, open_context
from ragcentre.models import api
from ragcentre.ocr import addresses, discovery, monitor, probe
from ragcentre.providers.parsers import http as parser_http
from ragcentre.settings import Settings

READ = "RAGCentre OCR check. This page is a scanned image. PUMP 4827. four bolts, inlet valve, pressure."
URL = "http://docling:8701"
HOST_URL = "http://host.docker.internal:8701"


@dataclass
class Shim:
    name: str = "docling-shim"
    version: str = "1.0"
    mimes: list[str] = field(default_factory=lambda: ["application/pdf", "image/png"])
    engine: dict[str, str] | None = None
    text: str = READ
    parse_status: int = 200
    refuse_pdf: bool = False
    jobs: bool = False
    key: str | None = None
    info_fault: str | None = None  # gone, slow, or an HTTP status
    parse_fault: bool = False
    parses: list[httpx.Request] = field(default_factory=list)
    looks: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return self.info(request)
        if self.key and request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}})
        if request.url.path == "/parse":
            self.parses.append(request)
            if self.parse_fault:
                raise httpx.ConnectError("reset", request=request)
            if self.jobs:
                return httpx.Response(202, json={"job_id": "j", "poll_after_seconds": 1})
            if self.refuse_pdf and b"%PDF" in request.content:
                return httpx.Response(415, json={"error": {"code": "unsupported_media_type", "message": "no"}})
            return self.result()
        if request.url.path.endswith("/result"):
            return self.result()
        if request.method == "DELETE":
            return httpx.Response(204)
        if self.parse_status >= 400:
            error = {"http_status": self.parse_status, "code": "failed", "message": "engine said no"}
            return httpx.Response(200, json={"status": "failed", "error": error})
        return httpx.Response(200, json={"status": "succeeded"})

    def info(self, request: httpx.Request) -> httpx.Response:
        self.looks.append(request)
        if self.info_fault == "gone":
            raise httpx.ConnectError("refused", request=request)
        if self.info_fault == "slow":
            raise httpx.ReadTimeout("slow", request=request)
        if self.info_fault:
            return httpx.Response(int(self.info_fault), headers={"location": "http://elsewhere:1/info"})
        body: dict[str, Any] = {
            "protocol": "ragcentre.parser/v1",
            "name": self.name,
            "version": self.version,
            "mime_types": self.mimes,
            "capabilities": {"async": self.jobs},
        }
        if self.engine is not None:
            body["engine"] = self.engine
        return httpx.Response(200, json=body)

    def result(self) -> httpx.Response:
        if self.parse_status >= 400:
            body = {"error": {"code": "failed", "message": "engine said no"}}
            return httpx.Response(self.parse_status, json=body)
        blocks = [{"type": "paragraph", "text": self.text}] if self.text else []
        return httpx.Response(200, json={"schema_version": "1.0", "blocks": blocks})


class World:
    """Shims by URL, a clock, and background work that runs when told to."""

    def __init__(self, tmp: Path, *, container: bool = True, **settings: Any) -> None:
        self.shims: dict[str, Shim] = {}
        self.now = 1000.0
        self.pending: list[Callable[[], None]] = []
        self.client_options: list[dict[str, Any]] = []
        self.ctx: AppContext = open_context(Settings(data_dir=tmp, **settings), http_client_factory=self.client)
        self.monitor = monitor.OcrMonitor(
            self.ctx, clock=lambda: self.now, spawn=self.pending.append, container=container
        )
        self.ctx._services["ocr"] = self.monitor

    def client(self, **options: Any) -> httpx.Client:
        self.client_options.append(options)
        return httpx.Client(transport=httpx.MockTransport(self.route), **options)

    def route(self, request: httpx.Request) -> httpx.Response:
        shim = self.shims.get(f"{request.url.scheme}://{request.url.netloc.decode()}")
        if shim is None:
            raise httpx.ConnectError("nothing listens", request=request)
        return shim.handle(request)

    def run(self) -> None:
        while self.pending:
            self.pending.pop(0)()

    def settle(self) -> list[dict[str, Any]]:
        for _ in range(8):
            self.monitor.snapshot()
            if not self.pending:
                break
            self.run()
        return self.monitor.snapshot()["services"]

    def later(self, seconds: float = 6.0) -> list[dict[str, Any]]:
        self.now += seconds
        return self.settle()

    def add(self, shim: Shim | None = None, url: str = URL, **config: Any) -> str:
        self.shims[url] = shim or Shim()
        return self.ctx.providers.create("docling", "parser", "http", {"base_url": url, **config}).id

    def one(self) -> dict[str, Any]:
        (service,) = self.settle()
        return service


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    monkeypatch.setattr(parser_http, "_sleep", lambda seconds: None)
    made = World(tmp_path)
    yield made
    made.ctx.close()


# ---- discovery


def test_nothing_running(world: World) -> None:
    assert world.settle() == []
    status = world.monitor.snapshot()
    assert status["discovery_enabled"] and not status["scanning"]


def test_a_found_shim_is_never_sent_a_document_or_a_credential(world: World) -> None:
    shim = world.shims[URL] = Shim()
    found = world.one()
    assert (found["state"], found["provider_id"], found["suggested_name"]) == ("detected", None, "docling")
    assert found["steps"] == {"shim": "ok", "engine": "unknown", "text": "unknown"}
    world.later(3600)
    assert shim.parses == []
    assert all("authorization" not in r.headers for r in shim.looks)
    assert {"trust_env": False} in world.client_options


@pytest.mark.parametrize("fault", ["302", "401", "404", "500", "slow"])
def test_what_is_not_a_shim_is_not_listed(world: World, fault: str) -> None:
    world.shims[URL] = Shim(info_fault=fault)
    assert world.settle() == []


@pytest.mark.parametrize(
    "body",
    [
        b"<html>",
        b"[]",
        b"null",
        b'{"protocol":"other","name":"x","version":"1"}',
        b'{"protocol":"ragcentre.parser/v1","name":"","version":"1"}',
        b'{"protocol":"ragcentre.parser/v1","name":"x","version":1}',
    ],
)
def test_info_that_is_not_the_protocol(body: bytes) -> None:
    assert discovery.read_info(body) is None


def test_info_ignores_malformed_optional_fields() -> None:
    body = b'{"protocol":"ragcentre.parser/v1","name":"x","version":"1","mime_types":5,"engine":{"state":[]}}'
    info = discovery.read_info(body)
    assert info and (info.mime_types, info.engine, info.instance_id) == ((), None, None)


def test_info_bodies_are_bounded() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (discovery.MAX_BODY + 1)))
    with httpx.Client(transport=transport) as client:
        assert discovery.look(client, "http://a:1").kind == "foreign"


def test_two_names_for_one_shim_are_one_entry(world: World) -> None:
    shim = Shim()
    world.shims.update({URL: shim, HOST_URL: shim})
    assert world.one()["base_url"] == URL
    world.ctx.providers.create("docling", "parser", "http", {"base_url": HOST_URL})
    service = world.one()
    assert (service["base_url"], service["state"]) == (HOST_URL, "ready")


def test_a_registered_shim_never_flashes_as_a_new_find(world: World) -> None:
    shim = Shim()
    world.shims[URL] = shim
    world.add(shim, HOST_URL)
    world.monitor.snapshot()
    # Only the discovery look of the other name has answered so far.
    world.monitor._found[URL].online = True
    world.monitor._found[URL].info = discovery.ShimInfo("docling-shim", "1.0")
    assert [s["base_url"] for s in world.monitor.snapshot()["services"]] == [HOST_URL]


def test_different_instances_are_two_entries(world: World) -> None:
    world.shims[URL] = Shim(engine={"instance_id": "a"})
    world.shims[HOST_URL] = Shim(engine={"instance_id": "b"})
    assert len(world.settle()) == 2


def test_a_remote_shim_is_not_merged_with_a_local_one(world: World) -> None:
    world.add(Shim(), "http://gpu-box:8701")
    world.shims[URL] = Shim()
    assert [s["state"] for s in world.settle()] == ["ready", "detected"]


def test_suggested_name_avoids_taken_names(world: World) -> None:
    world.add(Shim(), "http://gpu-box:9000")
    world.shims[URL] = Shim()
    assert world.settle()[1]["suggested_name"] == "docling-2"
    assert monitor.suggest_name("7 Segments!", set()) == "ocr"


def test_candidate_addresses() -> None:
    native = addresses.candidate_urls("auto", container=False)
    docker = addresses.candidate_urls("auto", container=True)
    assert "http://127.0.0.1:8701" in native and not any("docling" in u for u in native)
    assert URL in docker and HOST_URL in docker and not any("127.0.0.1" in u for u in docker)
    assert addresses.candidate_urls("off", container=True) == []
    assert addresses.candidate_urls("http://gpu:9/ , ftp://x, http://u:p@h:1", container=False) == ["http://gpu:9"]
    assert addresses.candidate_urls("auto,http://gpu:9", container=False)[-1] == "http://gpu:9"
    assert addresses.candidate_urls("http://h:99999", container=False) == []


def test_discovery_settings_come_from_the_environment() -> None:
    settings = Settings.from_env({"RAGCENTRE_DISCOVERY_URLS": "off", "RAGCENTRE_OCR_RECHECK_SECONDS": "0"})
    assert (settings.discovery_urls, settings.ocr_recheck_seconds) == ("off", 0)


# ---- readiness


@pytest.mark.parametrize("jobs", [False, True])
def test_ready_means_the_page_was_read(world: World, jobs: bool) -> None:
    shim = Shim(jobs=jobs)
    world.add(shim)
    service = world.one()
    assert service["state"] == "ready" and service["checked_at"]
    assert service["steps"] == {"shim": "ok", "engine": "ok", "text": "ok"}
    assert len(shim.parses) == 1 and probe.page_pdf() in shim.parses[0].content


@pytest.mark.parametrize("jobs", [False, True])
@pytest.mark.parametrize(
    ("status", "state", "engine"),
    [(503, "engine_down", "failed"), (502, "engine_down", "failed"), (422, "failed", "failed"),
     (429, "failed", "unknown"), (401, "needs_key", "unknown")],
)  # fmt: skip
def test_failed_parses_are_told_apart(world: World, jobs: bool, status: int, state: str, engine: str) -> None:
    world.add(Shim(jobs=jobs, parse_status=status))
    service = world.one()
    assert (service["state"], service["steps"]["engine"]) == (state, engine)
    assert str(status) in service["detail"]
    assert "engine said no" not in service["detail"]


@pytest.mark.parametrize("text", ["", ".", "4827", "pump valve bolts pressure", "check.pdf"])
def test_an_answer_without_the_pages_text_is_not_ready(world: World, text: str) -> None:
    world.add(Shim(text=text))
    service = world.one()
    assert service["state"] == "failed"
    assert service["steps"] == {"shim": "ok", "engine": "unknown", "text": "failed"}


def test_a_misread_letter_still_passes() -> None:
    assert probe.text_was_read(READ.replace("image", "lmage").replace("valve", "va1ve"))


def test_the_probe_polls_quickly(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(parser_http, "_sleep", waits.append)
    world.add(Shim(jobs=True))
    world.settle()
    assert waits and max(waits) <= probe.POLL_SECONDS


def test_an_image_only_shim_gets_the_png(world: World) -> None:
    shim = Shim(mimes=["image/*"])
    world.add(shim)
    assert world.one()["state"] == "ready" and probe.page_png() in shim.parses[0].content


def test_a_refused_pdf_falls_back_to_png(world: World) -> None:
    shim = Shim(mimes=[], refuse_pdf=True)
    world.add(shim)
    assert world.one()["state"] == "ready" and len(shim.parses) == 2


def test_a_parser_without_ocr_formats(world: World) -> None:
    shim = Shim(mimes=["text/html"])
    world.add(shim)
    assert world.one()["state"] == "no_ocr" and shim.parses == []


def test_lost_connection_during_the_probe(world: World) -> None:
    world.add(Shim(parse_fault=True))
    service = world.one()
    assert (service["state"], service["steps"]["engine"]) == ("failed", "unknown")


def test_checking_is_visible_while_the_probe_runs(world: World) -> None:
    world.add()
    world.monitor.snapshot()
    world.run()  # the looks
    service = world.monitor.snapshot()["services"][0]  # starts the probe
    assert (service["state"], service["steps"]["engine"]) == ("checking", "running")


def test_one_probe_at_a_time(world: World) -> None:
    provider = world.add()
    world.monitor.snapshot()
    world.run()
    for _ in range(4):
        world.monitor.snapshot()
        world.monitor.check(provider)
    world.run()
    assert len(world.shims[URL].parses) == 1


# ---- over time


def test_failure_retries_with_backoff_and_recovers(world: World) -> None:
    shim = Shim(parse_status=503)
    world.add(shim)
    assert world.one()["state"] == "engine_down"
    world.later(10)
    assert len(shim.parses) == 1
    world.later(25)
    assert len(shim.parses) == 2
    world.later(40)
    assert len(shim.parses) == 2  # the second wait is longer
    shim.parse_status = 200
    assert world.later(30)[0]["state"] == "ready"


def test_a_retry_keeps_the_last_result_on_screen(world: World) -> None:
    world.add(Shim(text=""))
    world.one()
    world.now += 31
    service = world.monitor.snapshot()["services"][0]
    assert (service["state"], service["rechecking"]) == ("failed", True)


def test_success_is_rechecked_so_a_dead_engine_is_noticed(world: World) -> None:
    shim = Shim()
    world.add(shim)
    assert world.one()["state"] == "ready"
    shim.text = ""  # /info looks exactly the same
    assert world.later(300)[0]["state"] == "ready"
    assert world.later(301)[0]["state"] == "failed"


def test_recheck_can_be_turned_off(tmp_path: Path) -> None:
    world = World(tmp_path, ocr_recheck_seconds=0)
    shim = Shim()
    world.add(shim)
    world.settle()
    world.later(100_000)
    assert len(shim.parses) == 1
    world.ctx.close()


def test_offline_takes_two_misses_and_a_return_is_proven_again(world: World) -> None:
    shim = Shim()
    world.add(shim)
    world.settle()
    shim.info_fault = "gone"
    assert world.later()[0]["state"] == "ready"
    offline = world.later()[0]
    assert (offline["state"], offline["checked_at"], offline["steps"]["shim"]) == ("offline", None, "failed")
    shim.info_fault = None
    assert world.later()[0]["state"] == "ready" and len(shim.parses) == 2


def test_busy_is_not_offline(world: World) -> None:
    shim = Shim()
    world.add(shim)
    world.settle()
    shim.info_fault = "slow"
    for _ in range(5):
        service = world.later()[0]
        assert (service["state"], service["busy"]) == ("ready", True)
    assert world.later(120)[0]["state"] == "offline"


def test_a_restart_between_looks_is_noticed(world: World) -> None:
    shim = Shim(engine={"state": "idle", "instance_id": "one"})
    world.add(shim)
    world.settle()
    shim.engine = {"state": "idle", "instance_id": "two"}
    shim.text = ""
    assert world.later()[0]["state"] == "failed" and len(shim.parses) == 2


def test_unreachable_remote_engine(world: World) -> None:
    shim = Shim(engine={"state": "ready"})
    world.add(shim)
    world.settle()
    shim.engine = {"state": "unreachable"}
    service = world.later()[0]
    assert (service["state"], service["steps"]["shim"], service["steps"]["engine"]) == ("engine_down", "ok", "failed")
    assert len(shim.parses) == 1
    shim.engine = {"state": "ready"}
    assert world.later()[0]["state"] == "ready" and len(shim.parses) == 2


def test_unreachable_engine_wins_over_version_drift(world: World) -> None:
    shim = Shim()
    world.add(shim)
    shim.version, shim.engine = "1.0.remote", {"state": "unreachable"}
    assert world.one()["state"] == "engine_down"


def test_an_engine_error_from_a_real_document_is_checked_at_once(world: World) -> None:
    shim = Shim(engine={"state": "ready"})
    world.add(shim)
    world.settle()
    shim.engine, shim.parse_status = {"state": "error"}, 503
    assert world.later()[0]["state"] == "engine_down" and len(shim.parses) == 2


def test_version_drift(world: World) -> None:
    shim = Shim()
    world.add(shim)
    world.settle()
    shim.version = "2.0"
    service = world.later()[0]
    assert service["state"] == "changed" and "2.0" in service["detail"] and len(shim.parses) == 1


def test_drift_allowed_by_the_provider(world: World) -> None:
    shim = Shim()
    world.add(shim, on_version_drift="warn")
    shim.version = "2.0"
    assert world.one()["state"] == "ready"


def test_another_service_with_the_same_version(world: World) -> None:
    shim = Shim()
    world.add(shim, on_version_drift="warn")
    shim.name = "someone-else"
    service = world.one()
    assert service["state"] == "changed" and shim.parses == []


def test_setting_the_key_tests_again(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = world.add(Shim(key="right-key"))
    assert world.one()["state"] == "needs_key"
    monkeypatch.setenv("RAGCENTRE_PROVIDER_OCR_TEST_KEY", "right-key")
    world.ctx.providers.set_secret(provider, "api_key", env="RAGCENTRE_PROVIDER_OCR_TEST_KEY")
    assert world.one()["state"] == "ready"
    assert "right-key" not in str(world.monitor.snapshot())
    world.later()
    # The key travels only with the test (whose parser client reads /info once), never with the polling.
    assert "authorization" not in world.shims[URL].looks[-1].headers


def test_a_missing_key_variable(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = world.add()
    monkeypatch.delenv("RAGCENTRE_PROVIDER_OCR_NOT_SET", raising=False)
    world.ctx.providers.set_secret(provider, "api_key", env="RAGCENTRE_PROVIDER_OCR_NOT_SET")
    service = world.one()
    assert service["state"] == "needs_key" and "RAGCENTRE_PROVIDER_OCR_NOT_SET" in service["detail"]


def test_a_result_for_an_edited_provider_is_discarded(world: World) -> None:
    provider = world.add()
    world.monitor.snapshot()
    world.run()
    world.monitor.snapshot()  # the probe is queued
    world.ctx.providers.update(provider, {"timeout_seconds": 30})
    assert world.monitor.snapshot()["services"][0]["checked_at"] is None
    world.run()
    assert world.one()["state"] == "ready"


def test_manual_check(world: World) -> None:
    shim = Shim()
    provider = world.add(shim)
    world.settle()
    assert world.monitor.check(provider)["services"][0]["state"] == "checking"
    world.run()
    assert world.one()["state"] == "ready" and len(shim.parses) == 2


def test_manual_check_can_retry_an_unreachable_engine(world: World) -> None:
    shim = Shim(engine={"state": "unreachable"})
    provider = world.add(shim)
    world.settle()
    assert shim.parses == []
    world.monitor.check(provider)
    world.run()
    assert len(shim.parses) == 1


def test_deleting_the_provider(world: World) -> None:
    provider = world.add()
    world.settle()
    world.ctx.providers.delete(provider)
    assert world.one()["state"] == "detected"


def test_localhost_inside_a_container_gets_the_fix(world: World) -> None:
    world.add(url="http://localhost:8701")
    world.shims.clear()
    service = world.one()
    assert service["state"] == "offline" and HOST_URL in service["message"]


def test_no_hint_outside_a_container(tmp_path: Path) -> None:
    world = World(tmp_path, container=False)
    world.add(url="http://localhost:8701")
    world.shims.clear()
    assert "host.docker.internal" not in world.one()["message"]
    world.ctx.close()


# ---- the page and the API


def test_the_probe_pdf_is_only_pixels() -> None:
    pdf = probe.page_pdf()
    assert pdf.startswith(b"%PDF") and b"/Font" not in pdf and b" BT" not in pdf
    image = pdf.split(b"/Subtype /Image", 1)[1]
    size = re.search(rb"/Width (\d+) /Height (\d+)", image)
    assert size
    width, height = int(size[1]), int(size[2])
    data = image.split(b"stream\n", 1)[1].split(b"\nendstream", 1)[0]
    assert len(zlib.decompress(data)) == (width + 1) * height  # one predictor byte per row


def test_probe_xref_offsets() -> None:
    pdf = probe.page_pdf()
    for number, found in enumerate(re.finditer(rb"(\d{10}) 00000 n ", pdf), start=1):
        assert pdf[int(found[1]) :].startswith(b"%d 0 obj" % number)


def test_monitor_and_api_agree_on_the_states() -> None:
    assert get_args(monitor.OcrState) == get_args(api.OcrState)
    assert set(monitor.STEPS) == set(get_args(api.OcrState))


def test_api(world: World) -> None:
    provider = world.add()
    with TestClient(create_app(world.ctx)) as client:
        assert client.get("/v1/ocr-services").json()["scanning"] is True
        world.settle()
        assert client.get("/v1/ocr-services").json()["services"][0]["state"] == "ready"
        assert client.post("/v1/ocr-services/scan").status_code == 200
        world.run()
        assert client.post(f"/v1/providers/{provider}/ocr-check").status_code == 202
        assert client.post("/v1/providers/nope/ocr-check").status_code == 404
        builtin = world.ctx.providers.by_name("builtin-text")
        assert builtin and client.post(f"/v1/providers/{builtin.id}/ocr-check").status_code == 409


def test_api_needs_the_token(tmp_path: Path) -> None:
    world = World(tmp_path, api_token="api-token-for-test")
    with TestClient(create_app(world.ctx)) as client:
        assert client.get("/v1/ocr-services").status_code == 401
        assert client.post("/v1/ocr-services/scan").status_code == 401
    assert world.pending == []
    world.ctx.close()
