"""Opt-in ingestion and strategy loop against a real HTTP OCR service.

OCR_LIVE_WORKFLOW_URL=http://127.0.0.1:18710 uv run pytest tests/ocr_live/test_user_workflow_live.py -q
Defaults target Tesseract's language option. Set OCR_LIVE_WORKFLOW_OPTIONS and
OCR_LIVE_WORKFLOW_CHANGED_OPTIONS to JSON objects for another engine.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ocr_contract_support import Host, pipeline_body, route
from ragcentre.api.app import create_app
from ragcentre.context import default_http_client_factory, open_context
from ragcentre.settings import Settings

URL = os.environ.get("OCR_LIVE_WORKFLOW_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set OCR_LIVE_WORKFLOW_URL to a real OCR shim URL")


def test_real_image_ingestion_query_and_strategy_cache(tmp_path):
    options = json.loads(os.environ.get("OCR_LIVE_WORKFLOW_OPTIONS", '{"lang":"eng"}'))
    changed_options = json.loads(os.environ.get("OCR_LIVE_WORKFLOW_CHANGED_OPTIONS", '{"lang":"eng+osd"}'))
    assert options != changed_options, "the strategy loop needs two different option sets"
    sent: list[bytes] = []

    def observe(request):
        if request.method == "POST" and request.url.path.endswith("/parse"):
            sent.append(request.read())

    def clients(**kwargs):
        return default_http_client_factory(event_hooks={"request": [observe]}, **kwargs)

    settings = Settings(
        data_dir=tmp_path / "data",
        workers=0,
        discovery_urls="off",
        idle_sleep_seconds=0.01,
        max_idle_sleep_seconds=0.02,
    )
    ctx = open_context(settings, http_client_factory=clients)
    try:
        with TestClient(create_app(ctx)) as api:
            host = Host(api, ctx, None)
            provider = host.add_parser("live-ocr", base_url=URL)
            assert provider["config"]["service_name"]
            assert provider["config"]["service_version"]
            fixture = Path(__file__).parent / "fixtures/scan_clean.png"
            doc = host.upload("scan.png", fixture.read_bytes(), "image/png")
            note = host.upload("note.txt", b"This plain text uses the builtin parser.", "text/plain")

            def pipeline(name, strategy):
                return host.pipeline(
                    pipeline_body(name, [route("text/*", "builtin-text"), route("image/*", "live-ocr", **strategy)])
                )

            a = pipeline("original", options)
            assert host.parse_run(doc["id"], a)["status"] == "succeeded"
            assert host.parse_run(note["id"], a)["status"] == "succeeded"
            assert len(sent) == 1, "plain text must bypass OCR"
            assert json.dumps(options).encode() in sent[0]
            assert any("warehouse" in text.lower() for text in host.texts(doc["id"], a))
            assert any("warehouse" in text.lower() for text in host.query(a, "warehouse inspection"))
            assert host.parse_run(doc["id"], a)["stats"]["cache_hit"] is False

            b = pipeline("changed", changed_options)
            assert host.parse_run(doc["id"], b)["status"] == "succeeded"
            assert host.parse_node_fp(a) != host.parse_node_fp(b)
            assert host.parse_run(doc["id"], b)["stats"]["cache_hit"] is False
            assert len(sent) == 2
            assert json.dumps(changed_options).encode() in sent[1]
            assert host.parsed(doc["id"], b)["blocks"]

            response = api.put(f"/v1/corpora/{host.corpus_id}/active-pipeline", json={"pipeline_id": a})
            assert response.status_code == 200, response.text
            assert host.retry_parse(doc["id"], a)["stats"]["cache_hit"] is True
            assert len(sent) == 2, "returning to the original strategy must reuse its parse"
    finally:
        ctx.close()


@pytest.mark.skipif(
    not os.environ.get("OCR_LIVE_CONTENT_EFFECT_OFF_OPTIONS"),
    reason="set OCR_LIVE_CONTENT_EFFECT_OFF_OPTIONS and OCR_LIVE_CONTENT_EFFECT_ON_OPTIONS for a real OCR toggle",
)
def test_strategy_changes_the_text_users_can_retrieve(tmp_path):
    off = json.loads(os.environ["OCR_LIVE_CONTENT_EFFECT_OFF_OPTIONS"])
    on = json.loads(os.environ["OCR_LIVE_CONTENT_EFFECT_ON_OPTIONS"])
    settings = Settings(
        data_dir=tmp_path / "data",
        workers=0,
        discovery_urls="off",
        idle_sleep_seconds=0.01,
        max_idle_sleep_seconds=0.02,
    )
    ctx = open_context(settings)
    try:
        with TestClient(create_app(ctx)) as api:
            host = Host(api, ctx, None)
            host.add_parser("live-ocr", base_url=URL)
            fixture = Path(__file__).parent / "fixtures/scan_clean.png"
            doc = host.upload("scan.png", fixture.read_bytes(), "image/png")
            a = host.pipeline(pipeline_body("without-ocr", [route("image/*", "live-ocr", **off)]), timeout=180)
            assert host.parse_run(doc["id"], a)["status"] == "succeeded"
            assert not any("warehouse" in text.lower() for text in host.texts(doc["id"], a))
            # ocr off yields no text, so this strategy indexes nothing: the query cannot surface "warehouse",
            # whether it answers "no hits" (200) or "the index was never built" (409).
            r = host.client.post(
                f"/v1/corpora/{host.corpus_id}/query", json={"query": "warehouse inspection", "pipeline_id": a}
            )
            assert r.status_code in (200, 409), r.text
            if r.status_code == 200:
                assert not any("warehouse" in str(hit["text"]).lower() for hit in r.json()["results"])
            b = host.pipeline(pipeline_body("with-ocr", [route("image/*", "live-ocr", **on)]), timeout=180)
            assert host.parse_run(doc["id"], b)["status"] == "succeeded"
            assert host.parse_node_fp(a) != host.parse_node_fp(b)
            assert any("warehouse" in text.lower() for text in host.texts(doc["id"], b))
            assert any("warehouse" in text.lower() for text in host.query(b, "warehouse inspection"))
    finally:
        ctx.close()
