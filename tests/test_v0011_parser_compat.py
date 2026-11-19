"""Parser compatibility fixes: MIME guessing on upload (A3) and page images in the parse cache (A4)."""

from __future__ import annotations

import json
import time
from typing import Any

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
