"""``ragcentre parser check`` and ``POST /providers/{id}/test?deep=true`` (D1)."""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ragcentre.cli import app as cli_app
from ragcentre.context import AppContext
from ragcentre.providers.parsers.check import check_parser
from ragcentre.providers.parsers.sample import HEADER, sample_pdf
from ragcentre.testing.fakes import FakeServices


def good_ir() -> dict[str, Any]:
    """What a good parser returns for the built-in sample."""

    def block(type_: str, text: str, page: int, y: float, **extra: Any) -> dict[str, Any]:
        return {"type": type_, "text": text, "page": page, "bbox": [72.0, y, 400.0, y + 14], **extra}

    return {
        "schema_version": "1.0",
        "metadata": {},
        "pages": [{"page": 1, "width": 612.0, "height": 792.0}, {"page": 2, "width": 612.0, "height": 792.0}],
        "blocks": [
            block("page_header", HEADER, 1, 20),
            block("heading", "Pump Manual", 1, 60, level=1),
            block("heading", "Installation", 1, 130, level=2),
            block("paragraph", "Mount the pump on a level surface and secure it with four bolts.", 1, 170),
            block("list_item", "Close the inlet valve.", 1, 200),
            block("list_item", "Connect the outlet hose.", 1, 218),
            block("page_footer", "Page 1", 1, 740),
            block("page_header", HEADER, 2, 20),
            block("heading", "Specifications", 2, 80, level=2),
            block("paragraph", "The table lists the rated values of the pump.", 2, 110),
            block("table", "| Parameter | Value |\n|---|---|\n| Flow | 40 l/min |\n| Pressure | 4 bar |", 2, 150),
            block("figure", "Figure 1: exploded view", 2, 240),
            block("page_footer", "Page 2", 2, 740),
        ],
    }


def service(
    ir: dict[str, Any] | None = None,
    *,
    protocol: str = "ragcentre.parser/v1",
    async_jobs: bool = False,
    bad_file_status: int = 415,
    info_needs_key: str | None = None,
    parse_key: str | None = None,
) -> TestClient:
    ir = ir if ir is not None else good_ir()
    api = FastAPI()
    jobs: dict[str, dict[str, Any]] = {}

    def denied(request: Request, key: str | None) -> JSONResponse | None:
        if key is not None and request.headers.get("authorization") != f"Bearer {key}":
            return JSONResponse({"error": {"code": "unauthorized", "message": "no"}}, status_code=401)
        return None

    @api.get("/info")
    def info(request: Request) -> Any:
        if (resp := denied(request, info_needs_key)) is not None:
            return resp
        caps: dict[str, Any] = {"page_images": False, **({"async": True} if async_jobs else {})}
        return {"protocol": protocol, "name": "demo", "version": "1.0", "mime_types": ["application/pdf"],
                "capabilities": caps}  # fmt: skip

    @api.post("/parse")
    def parse(request: Request, file: UploadFile = File(...), mode: str = "") -> Any:
        if (resp := denied(request, parse_key)) is not None:
            return resp
        if file.filename and file.filename.endswith(".xyz"):
            if bad_file_status == 200:
                return ir
            body: Any = {"error": {"code": "bad", "message": "cannot read"}} if bad_file_status < 500 else "oops"
            return JSONResponse(body, status_code=bad_file_status)
        if async_jobs and mode == "async":
            jobs["j1"] = {"ir": ir, "polls": 0}
            return JSONResponse({"job_id": "j1", "poll_after_seconds": 0.2}, status_code=202)
        return ir

    @api.get("/jobs/{job_id}")
    def job(job_id: str) -> Any:
        record = jobs[job_id]
        record["polls"] += 1
        return {"status": "running" if record["polls"] < 2 else "succeeded"}

    @api.get("/jobs/{job_id}/result")
    def result(job_id: str) -> Any:
        return jobs[job_id]["ir"]

    @api.delete("/jobs/{job_id}", status_code=204)
    def delete(job_id: str) -> None:
        jobs.pop(job_id, None)

    return TestClient(api)


def run(client: TestClient, **kwargs: Any) -> Any:
    return check_parser(client, "http://testserver", sleep=lambda seconds: None, **kwargs)


def by_check(report: Any, level: str) -> dict[str, Any]:
    return {f.check: f for f in report.findings if f.level == level}


def test_a_good_service_passes_clean() -> None:
    report = run(service())
    assert report.ok and not [f for f in report.findings if f.level != "info"], report.as_dict()
    assert report.service == {"name": "demo", "version": "1.0"} and report.blocks == 13 and report.pages == 2


def test_an_async_service_is_checked_through_a_job() -> None:
    report = run(service(async_jobs=True))
    assert report.ok and not by_check(report, "warn")
    assert any("async" in f.message for f in report.findings)


def test_the_sample_pdf_is_a_pdf() -> None:
    pdf = sample_pdf()
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF") and b"Specifications" in pdf


def test_unreachable() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("no"))))
    report = check_parser(client, "http://nowhere")
    assert not report.ok and "cannot reach" in report.findings[0].message


def test_wrong_protocol_is_an_error() -> None:
    report = run(service(protocol="ragcentre.parser/v2"))
    assert not report.ok and "ragcentre.parser/v1" in by_check(report, "error")["info"].message


def test_info_behind_auth_cannot_be_registered() -> None:
    client = service(info_needs_key="k")
    client.headers["Authorization"] = "Bearer k"
    report = run(client)
    assert not report.ok and "cannot be registered" in by_check(report, "error")["info"].message


def test_wrong_credentials() -> None:
    client = service(info_needs_key="k")
    client.headers["Authorization"] = "Bearer wrong"
    assert not run(client).ok


def test_invalid_ir_is_an_error() -> None:
    bad = good_ir()
    bad["blocks"][1].pop("level")  # a heading without a level
    report = run(service(bad))
    assert not report.ok and "invalid" in by_check(report, "error")["ir"].message


def test_parse_rejected() -> None:
    report = run(service(parse_key="k"))
    assert not report.ok and "401" in by_check(report, "error")["parse"].message


@pytest.mark.parametrize(
    ("mutate", "check", "lost"),
    [
        (lambda ir: [b.update(level=2) for b in ir["blocks"] if b["type"] == "heading"], "headings", "structure chunker"),
        (lambda ir: ir["blocks"].__setitem__(slice(None), [b for b in ir["blocks"] if b["type"] != "heading"]),
         "headings", "heading paths"),
        (lambda ir: [b.pop("page") for b in ir["blocks"]], "pages", "page chunker"),
        (lambda ir: [b.update(text="Flow 40 l/min Pressure 4 bar") for b in ir["blocks"] if b["type"] == "table"],
         "tables", "table-aware"),
        (lambda ir: [b.update(type="paragraph") for b in ir["blocks"] if b["type"].startswith("page_")],
         "header_footer", "header and footer removal"),
        (lambda ir: ir["blocks"][3].update(bbox=[0, 0, 5000, 5000]), "bbox", "box-based"),
        (lambda ir: ir["blocks"].reverse(), "reading_order", "reading order"),
        (lambda ir: ir["blocks"].__setitem__(slice(None), [b for b in ir["blocks"] if "Installation" not in b["text"]]),
         "content", "the text itself"),
    ],
)  # fmt: skip
def test_warnings_name_the_feature_lost(mutate: Any, check: str, lost: str) -> None:
    ir = copy.deepcopy(good_ir())
    mutate(ir)
    report = run(service(ir))
    assert report.ok, report.as_dict()
    finding = by_check(report, "warn").get(check)
    assert finding is not None and finding.lost is not None and lost in finding.lost, report.as_dict()


def test_flat_headings_need_three_to_count() -> None:
    ir = good_ir()
    for b in ir["blocks"]:
        if b["type"] == "heading":
            b["level"] = 1
    assert "headings" in by_check(run(service(ir)), "warn")
    ir2 = {**ir, "blocks": [b for b in ir["blocks"] if b["text"] != "Specifications"]}
    assert "headings" not in by_check(run(service(ir2)), "warn")


def test_server_errors_for_bad_files_are_warned() -> None:
    warn = by_check(run(service(bad_file_status=500)), "warn")["errors"]
    assert "retries" in warn.message and "415 or 422" in (warn.lost or "")
    assert "errors" not in by_check(run(service(bad_file_status=415)), "warn")
    assert "errors" not in by_check(run(service(bad_file_status=200)), "warn")
    assert "errors" not in by_check(run(service(bad_file_status=500), probe_errors=False), "warn")


def test_a_file_of_your_own() -> None:
    seen: list[str] = []
    client = service()
    original = client.build_request

    def spy(method: str, url: Any, **kwargs: Any) -> Any:
        if method == "POST" and "files" in kwargs:
            seen.append(kwargs["files"]["file"][0])
        return original(method, url, **kwargs)

    client.build_request = spy  # type: ignore[method-assign]
    report = run(client, file=("scan.png", b"\x89PNG", "image/png"), probe_errors=False)
    assert seen == ["scan.png"] and report.ok
    assert any("does not list image/png" in f.message for f in report.findings)
    # No reading-order checks for a file whose content is unknown.
    assert "reading_order" not in by_check(run(service(), file=("a.pdf", b"x", "application/pdf")), "warn")


# --- CLI and API -------------------------------------------------------------------------------


def test_cli_reports_an_unreachable_service() -> None:
    result = CliRunner().invoke(cli_app, ["parser", "check", "http://127.0.0.1:9", "--timeout", "2"])
    assert result.exit_code == 1 and "FAIL" in result.output and "NOT usable" in result.output


def test_cli_needs_the_named_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE_KEY", raising=False)
    result = CliRunner().invoke(cli_app, ["parser", "check", "http://127.0.0.1:9", "--api-key-env", "NOPE_KEY"])
    assert result.exit_code == 1 and "NOPE_KEY" in result.output


def test_cli_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = service()
    monkeypatch.setattr(
        "ragcentre.providers.parsers.check.check_parser",
        lambda client, base_url, **kwargs: check_parser(ok, base_url, sleep=lambda s: None, **kwargs),
    )
    result = CliRunner().invoke(cli_app, ["--json", "parser", "check", "http://testserver"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True
    text = CliRunner().invoke(cli_app, ["parser", "check", "http://testserver"])
    assert "usable, 0 warning(s)" in text.output


def test_deep_provider_test(ctx: AppContext, client: TestClient, fakes: FakeServices) -> None:
    from ragcentre.testing.fakes import PARSER_URL

    ctx.providers.create("docling", "parser", "http", {"base_url": PARSER_URL})
    provider_id = ctx.providers.by_name("docling").id
    shallow = client.post(f"/v1/providers/{provider_id}/test").json()
    assert shallow["ok"] is True and "check" not in shallow  # the shallow response is unchanged
    deep = client.post(f"/v1/providers/{provider_id}/test", params={"deep": "true"}).json()
    assert deep["ok"] is True and deep["check"]["ok"] is True and deep["check"]["blocks"] > 0
    assert fakes.count("parser", "/parse") >= 1


def test_deep_provider_test_reports_errors(ctx: AppContext, client: TestClient, fakes: FakeServices) -> None:
    from ragcentre.testing.fakes import PARSER_URL

    fakes.parse_override = lambda content, mime, options: {"schema_version": "9.0", "blocks": []}
    ctx.providers.create("docling", "parser", "http", {"base_url": PARSER_URL})
    deep = client.post(f"/v1/providers/{ctx.providers.by_name('docling').id}/test", params={"deep": "true"}).json()
    assert deep["ok"] is False and "invalid" in deep["message"]
    assert deep["check"]["findings"][-1]["level"] == "error"


# --- connection resets are retried on a fresh connection ------------------------------------------


def test_send_retries_a_dropped_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from ragcentre.providers.parsers import check as check_mod

    monkeypatch.setattr(check_mod, "retry_sleep", lambda seconds: None)
    calls: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadError("peer closed connection", request=request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resp = check_mod._send(client, "GET", "http://check.test/info")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert len(calls) == 2
    # the retry is a fresh connection: the client was closed in between
    client.close()


def test_send_does_not_retry_a_connection_error_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    from ragcentre.providers.parsers import check as check_mod

    monkeypatch.setattr(check_mod, "retry_sleep", lambda seconds: None)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadError("peer closed connection", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPError):
        check_mod._send(client, "POST", "http://check.test/parse")
    assert calls == check_mod.REQUEST_RETRIES + 1  # one attempt plus the retries


def test_send_does_not_retry_a_refused_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from ragcentre.providers.parsers import check as check_mod

    monkeypatch.setattr(check_mod, "retry_sleep", lambda seconds: None)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPError):
        check_mod._send(client, "GET", "http://check.test/info")
    assert calls == 1  # "cannot reach" is reported, not retried


def test_full_check_survives_a_reset_on_the_job_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    from ragcentre.providers.parsers import check as check_mod

    monkeypatch.setattr(check_mod, "retry_sleep", lambda seconds: None)
    async_client = service(async_jobs=True)
    original = async_client.send

    calls = 0

    def flaky(request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        nonlocal calls
        calls += 1
        if (
            request.url.path.startswith("/jobs/")
            and request.method == "GET"
            and not request.url.path.endswith("/result")
            and calls <= 1
        ):
            raise httpx.ReadError("peer closed connection", request=request)
        return original(request, *args, **kwargs)

    monkeypatch.setattr(async_client, "send", flaky)
    report = check_parser(async_client, "http://testserver", sleep=lambda s: None)
    assert report.ok, report.as_dict()
    assert any("async" in f.message for f in report.findings)
