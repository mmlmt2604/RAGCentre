"""Deep connection checks choose a sample the parser can read."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from ragcentre.providers.parsers.check import check_parser


@pytest.mark.parametrize("declared", [["image/png"], ["image/*"], ["image/png", "application/pdf"], []])
def test_default_sample_matches_the_services_media_types(declared: list[str]) -> None:
    app = FastAPI()
    received: list[tuple[str, str, bytes]] = []
    expect_png = bool(declared) and "application/pdf" not in declared

    @app.get("/info")
    def info() -> dict:
        return {"protocol": "ragcentre.parser/v1", "name": "image-reader", "version": "1", "mime_types": declared}

    @app.post("/parse")
    async def parse(file: UploadFile = File(...)):
        content = await file.read()
        received.append((file.filename or "", file.content_type or "", content))
        if expect_png and file.content_type != "image/png":
            return JSONResponse({"error": {"code": "unsupported_media_type"}}, status_code=415)
        return {
            "schema_version": "1.0",
            "metadata": {},
            "pages": [{"page": 1}],
            "blocks": [{"type": "paragraph", "text": "RAGCentre scanned pump 4827 bolts valve pressure", "page": 1}],
        }

    with TestClient(app) as client:
        report = check_parser(client, "http://testserver", probe_errors=False)
    assert report.ok, report.as_dict()
    assert len(received) == 1
    filename, mime, content = received[0]
    assert mime == ("image/png" if expect_png else "application/pdf")
    assert filename.endswith(".png" if expect_png else ".pdf")
    assert content.startswith(b"\x89PNG" if expect_png else b"%PDF")
    if expect_png:
        # The PDF's two-page layout checks do not apply to a one-page OCR image.
        assert not any(f.check == "sample" for f in report.findings)


def test_an_explicit_file_is_not_replaced_by_the_default_image_sample() -> None:
    sent: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"protocol": "ragcentre.parser/v1", "name": "images", "version": "1", "mime_types": ["image/png"]},
            )
        sent.append(request.read())
        return httpx.Response(415, json={"error": {"code": "unsupported_media_type"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        report = check_parser(
            client, "http://parser", file=("mine.pdf", b"my-pdf-file", "application/pdf"), probe_errors=False
        )
    assert not report.ok
    assert len(sent) == 1 and b"my-pdf-file" in sent[0]
