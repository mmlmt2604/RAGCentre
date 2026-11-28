"""Polling a slow OCR job survives a stale keep-alive connection."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import EngineClient


@pytest.mark.parametrize("failure", [httpx.ReadError, httpx.RemoteProtocolError])
def test_poll_and_result_reads_retry_without_submitting_the_document_twice(monkeypatch, tmp_path, failure):
    counts: dict[str, int] = {}

    def answer(request):
        key = request.method + " " + request.url.path
        counts[key] = counts.get(key, 0) + 1
        if request.url.path == "/parse":
            return httpx.Response(202, json={"job_id": "one"})
        if request.url.path == "/jobs/one" and request.method == "GET":
            if counts[key] == 1:
                raise failure("stale keep-alive", request=request)
            return httpx.Response(200, json={"status": "succeeded"})
        if request.url.path == "/jobs/one/result":
            if counts[key] == 1:
                raise failure("stale keep-alive", request=request)
            return httpx.Response(200, json={"blocks": [{"type": "paragraph", "text": "read"}]})
        if request.method == "DELETE":
            raise httpx.ConnectError("server stopped after result", request=request)
        raise AssertionError(key)

    monkeypatch.setattr("client.time.sleep", lambda _: None)
    scan = tmp_path / "scan.png"
    scan.write_bytes(b"pixels")
    with httpx.Client(base_url="http://shim", transport=httpx.MockTransport(answer)) as http:
        client = EngineClient("http://shim", http=http)
        reply = client.parse(scan, use_async=True)
    assert reply.status == 200 and reply.body["blocks"][0]["text"] == "read"
    assert counts["POST /parse"] == 1
    assert counts["GET /jobs/one"] == 2
    assert counts["GET /jobs/one/result"] == 2


def test_submission_is_not_retried_when_its_response_is_lost(tmp_path):
    submissions = []

    def answer(request):
        submissions.append(request)
        raise httpx.ReadError("response lost", request=request)

    scan = tmp_path / "scan.png"
    scan.write_bytes(b"pixels")
    with (
        httpx.Client(base_url="http://shim", transport=httpx.MockTransport(answer)) as http,
        pytest.raises(httpx.ReadError),
    ):
        EngineClient("http://shim", http=http).parse(scan, use_async=True)
    assert len(submissions) == 1
