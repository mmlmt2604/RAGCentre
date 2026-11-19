"""Parser compatibility fixes: MIME guessing on upload (A3) and page images in the parse cache (A4)."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ragcentre.api.routers.documents import _mime_type
from ragcentre.context import AppContext
from ragcentre.pipeline.planner import create_pipeline
from ragcentre.pipeline.status import status
from ragcentre.testing.fakes import FakeServices
from test_m6_ingestion import _add_providers, _corpus, _pipeline


class _Upload:
    def __init__(self, filename: str | None, content_type: str | None) -> None:
        self.filename = filename
        self.content_type = content_type


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("x.pdf", None, "application/pdf"),
        ("x.pdf", "", "application/pdf"),
        ("x.PDF", "application/octet-stream", "application/pdf"),
        ("x.png", "application/octet-stream; charset=binary", "image/png"),
        ("x.pdf", "application/x-custom", "application/x-custom"),
        ("x.md", "text/markdown", "text/markdown"),
        ("x.unknownext", "application/octet-stream", "application/octet-stream"),
        (None, None, "application/octet-stream"),
    ],
)
def test_mime_type_falls_back_to_filename(filename: str | None, content_type: str | None, expected: str) -> None:
    assert _mime_type(_Upload(filename, content_type)) == expected  # type: ignore[arg-type]


def test_upload_without_a_type_matches_the_pdf_route(ctx: AppContext, client: TestClient) -> None:
    corpus_id = _corpus(ctx)
    for method_path, doc in [(f"/v1/corpora/{corpus_id}/documents", None)]:
        r = client.post(
            method_path,
            files={"file": ("manual.pdf", b"%PDF-1.4 one", "application/octet-stream")},
            data={"metadata": "{}", "attributes": "{}"},
        )
        assert r.status_code == 201, r.text
        doc = r.json()
        assert doc["mime_type"] == "application/pdf"
    # Replacing the content re-guesses the type too.
    r = client.put(
        f"/v1/documents/{doc['id']}/content", files={"file": ("manual.txt", b"plain", "application/octet-stream")}
    )
    assert r.status_code in (200, 204), r.text
    assert ctx.db.scalar("SELECT mime_type FROM documents WHERE id = ?", (doc["id"],)) == "text/plain"


def test_parse_cache_drops_page_images(ctx: AppContext, client: TestClient, fakes: FakeServices) -> None:
    def parse(content: bytes, mime: str, options: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "metadata": {},
            "pages": [{"page": 1, "width": 10.0, "height": 10.0, "image": {"mime": "image/png", "data_b64": "AAAA"}}],
            "blocks": [{"type": "paragraph", "text": "Hello page one.", "page": 1}],
        }

    fakes.parse_override = parse
    _add_providers(ctx, http_parser=True)
    corpus_id = _corpus(ctx)
    r = client.post(
        f"/v1/corpora/{corpus_id}/documents",
        files={"file": ("a.md", b"# x", "text/markdown")},
        data={"metadata": "{}", "attributes": "{}"},
    )
    assert r.status_code == 201, r.text
    cfg = _pipeline(http_parser=True)
    cfg["nodes"][0]["routes"][0]["options"] = {"include_page_images": True}
    pid = create_pipeline(ctx, corpus_id, cfg)
    ctx.start_workers(2)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and status(ctx, pid).status != "ready":
            time.sleep(0.1)
    finally:
        ctx.close_workers()
    assert status(ctx, pid).status == "ready"
    sha = ctx.db.scalar("SELECT blob_sha256 FROM parse_cache")
    stored = ctx.blobs.get_json_gz(sha)
    assert stored["pages"] == [{"page": 1, "width": 10.0, "height": 10.0}]
    assert "image" not in json.dumps(stored)


# --- async parse jobs (B1) ------------------------------------------------------------------------

from ragcentre.errors import ExecutionError  # noqa: E402
from ragcentre.pipeline.executors import _ParseJob  # noqa: E402
from ragcentre.providers.parsers import http as http_parser  # noqa: E402
from ragcentre.testing.fakes import PARSER_URL  # noqa: E402


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(http_parser, "_sleep", slept.append)
    return slept


class MemoryJob:
    def __init__(self, job_id: str | None = None) -> None:
        self.job_id = job_id
        self.saved: list[str] = []

    def get(self) -> str | None:
        return self.job_id

    def save(self, job_id: str) -> None:
        self.job_id = job_id
        self.saved.append(job_id)

    def clear(self) -> None:
        self.job_id = None


def _parser(ctx: AppContext, **config: Any) -> Any:
    ctx.providers.create("p", "parser", "http", {"base_url": PARSER_URL, **config})
    return ctx.providers.instance(ctx.providers.by_name("p").id)


def test_sync_service_is_called_once(ctx: AppContext, fakes: FakeServices, sleeps: list[float]) -> None:
    ir = _parser(ctx).parse(b"hello", "a.pdf", "application/pdf", {})
    assert ir["blocks"] and fakes.count("parser", "/parse") == 1 and fakes.count("parser", "/jobs") == 0
    assert sleeps == []


def test_async_service_is_polled_until_done(ctx: AppContext, fakes: FakeServices, sleeps: list[float]) -> None:
    fakes.parser_async, fakes.job_running_polls = True, 3
    job = MemoryJob()
    ir = _parser(ctx).parse(b"hello", "a.pdf", "application/pdf", {}, job=job)
    assert ir["blocks"][0]["text"] == "hello"
    assert fakes.count("parser", "/parse") == 1 and fakes.count("parser", "/jobs") == 5  # 4 polls and the DELETE
    assert fakes.count("parser", "/jobs/result") == 1
    assert job.saved == ["job-1"] and job.job_id is None  # remembered while running, cleared after
    assert fakes.jobs == {}  # and deleted on the service
    assert sleeps[0] == 0.5 and sleeps == sorted(sleeps)  # backs off


def test_parse_outlives_the_request_timeout(ctx: AppContext, fakes: FakeServices, sleeps: list[float]) -> None:
    # timeout_seconds is per request; the job may take far longer (here 50 polls of up to 30 s).
    fakes.parser_async, fakes.job_running_polls = True, 50
    _parser(ctx, timeout_seconds=1, job_timeout_seconds=100000)
    ir = ctx.providers.instance(ctx.providers.by_name("p").id).parse(b"x", "a.pdf", "application/pdf", {})
    assert ir["blocks"] and sum(sleeps) > 600 and max(sleeps) <= 30


def test_a_restarted_worker_resumes_the_job(
    ctx: AppContext, fakes: FakeServices, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes.parser_async, fakes.job_running_polls = True, 2
    parser = _parser(ctx, job_timeout_seconds=1)
    provider_id = ctx.providers.by_name("p").id
    # First attempt: the wait runs out while the job is still running.
    clock = iter([0.0, 5.0])
    with monkeypatch.context() as m:
        m.setattr(http_parser.time, "monotonic", lambda: next(clock, 99.0))
        with pytest.raises(ExecutionError) as caught:
            parser.parse(b"hello", "a.pdf", "application/pdf", {}, job=_ParseJob(ctx, "key-1", provider_id))
    assert caught.value.retryable and "did not finish" in caught.value.message
    assert _ParseJob(ctx, "key-1", provider_id).get() == "job-1"
    # The retry (or another worker after a restart) polls the same job: nothing is submitted twice.
    parser = ctx.providers.instance(provider_id)
    ir = parser.parse(b"hello", "a.pdf", "application/pdf", {}, job=_ParseJob(ctx, "key-1", provider_id))
    assert ir["blocks"][0]["text"] == "hello"
    assert fakes.count("parser", "/parse") == 1
    assert _ParseJob(ctx, "key-1", provider_id).get() is None


def test_a_job_the_service_forgot_is_submitted_again(ctx: AppContext, fakes: FakeServices, sleeps: list[float]) -> None:
    fakes.parser_async = True
    parser = _parser(ctx)
    job = MemoryJob("job-gone")  # stored before the service restarted
    ir = parser.parse(b"hello", "a.pdf", "application/pdf", {}, job=job)
    assert ir["blocks"][0]["text"] == "hello"
    assert fakes.count("parser", "/parse") == 1 and job.saved == ["job-1"]


def test_failed_job_reports_the_service_error(ctx: AppContext, fakes: FakeServices, sleeps: list[float]) -> None:
    fakes.parser_async = True
    fakes.job_failure = {"http_status": 415, "code": "unsupported_media_type", "message": "no scans"}
    parser = _parser(ctx)
    job = MemoryJob()
    with pytest.raises(ExecutionError) as caught:
        parser.parse(b"x", "a.xyz", "application/octet-stream", {}, job=job)
    assert not caught.value.retryable and "no scans" in caught.value.message
    assert job.job_id is None  # a retry starts a fresh job
    fakes.job_failure = {"http_status": 503, "code": "busy", "message": "overloaded"}
    fakes.jobs.clear()
    with pytest.raises(ExecutionError) as caught:
        parser.parse(b"x", "a.pdf", "application/pdf", {}, job=job)
    assert caught.value.retryable and job.job_id is None


def test_a_service_that_ignores_async_still_works(ctx: AppContext, fakes: FakeServices, sleeps: list[float]) -> None:
    # It advertises async but answers ?mode=async with the IR itself.
    fakes.parser_async = True
    sync_ir = {"schema_version": "1.0", "metadata": {}, "pages": [], "blocks": [{"type": "paragraph", "text": "sync"}]}
    fakes.script("parser", "/parse", [httpx.Response(200, json=sync_ir)])
    ir = _parser(ctx).parse(b"x", "a.pdf", "application/pdf", {}, job=MemoryJob())
    assert ir["blocks"][0]["text"] == "sync" and fakes.jobs == {}


def test_async_end_to_end(ctx: AppContext, client: TestClient, fakes: FakeServices, sleeps: list[float]) -> None:
    fakes.parser_async, fakes.job_running_polls = True, 2
    _add_providers(ctx, http_parser=True)
    corpus_id = _corpus(ctx)
    r = client.post(
        f"/v1/corpora/{corpus_id}/documents",
        files={"file": ("a.md", b"# Title\n\nbody text", "text/markdown")},
        data={"metadata": "{}", "attributes": "{}"},
    )
    assert r.status_code == 201, r.text
    pid = create_pipeline(ctx, corpus_id, _pipeline(http_parser=True))
    ctx.start_workers(2)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and status(ctx, pid).status != "ready":
            time.sleep(0.1)
    finally:
        ctx.close_workers()
    assert status(ctx, pid).status == "ready"
    assert fakes.count("parser", "/parse") == 1
    assert ctx.db.scalar("SELECT COUNT(*) FROM parse_jobs") == 0
