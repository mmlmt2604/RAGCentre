from __future__ import annotations

import json

import pytest

from ragcentre.context import AppContext
from ragcentre.errors import ErrorCode, RagError
from ragcentre.ingest import ingest_document

DEFAULT_TEAM = "00000000-0000-0000-0000-000000000001"


def _corpus(ctx: AppContext, name: str = "c", schema: dict | None = None) -> str:
    ctx.db.execute(
        "INSERT INTO corpora (id, name, metadata_schema, team_id, created_at) VALUES (?, ?, ?, ?, ?)",
        ("corpus-1", name, json.dumps(schema or {}), DEFAULT_TEAM, "2026-01-01T00:00:00.000000Z"),
    )
    return "corpus-1"


def test_ingest_creates_then_upserts(ctx: AppContext) -> None:
    corpus_id = _corpus(ctx)
    first = ingest_document(ctx, corpus_id, b"hello", filename="a.txt", mime_type="text/plain", external_id="ext:1")
    assert first.created is True
    assert ctx.db.scalar("SELECT revision FROM documents WHERE id = ?", (first.document_id,)) == 1

    second = ingest_document(ctx, corpus_id, b"hello", filename="a.txt", mime_type="text/plain", external_id="ext:1")
    assert second.created is False
    assert second.document_id == first.document_id
    assert ctx.db.scalar("SELECT revision FROM documents WHERE id = ?", (first.document_id,)) == 1

    third = ingest_document(ctx, corpus_id, b"world", filename="a.txt", mime_type="text/plain", external_id="ext:1")
    assert third.created is False
    assert ctx.db.scalar("SELECT revision FROM documents WHERE id = ?", (first.document_id,)) == 2


def test_ingest_validates_metadata_against_schema(ctx: AppContext) -> None:
    corpus_id = _corpus(ctx, schema={"fields": {"title": {"type": "str", "default": ""}}})
    with pytest.raises(RagError) as exc:
        ingest_document(
            ctx, corpus_id, b"hello", filename="a.txt", mime_type="text/plain", metadata={"unknown": "x"}
        )
    assert exc.value.error_code is ErrorCode.VALIDATION_ERROR

    ok = ingest_document(
        ctx, corpus_id, b"hello", filename="a.txt", mime_type="text/plain", metadata={"title": "T"}
    )
    assert json.loads(ctx.db.scalar("SELECT metadata FROM documents WHERE id = ?", (ok.document_id,))) == {"title": "T"}


def test_ingest_rejects_oversize(ctx: AppContext) -> None:
    corpus_id = _corpus(ctx)
    ctx.settings = ctx.settings.with_(max_upload_mb=0)
    with pytest.raises(RagError) as exc:
        ingest_document(ctx, corpus_id, b"x", filename="a.txt", mime_type="text/plain")
    assert exc.value.error_code is ErrorCode.PAYLOAD_TOO_LARGE


def test_ingest_missing_corpus_is_404(ctx: AppContext) -> None:
    with pytest.raises(RagError) as exc:
        ingest_document(ctx, "no-such-corpus", b"x", filename="a.txt", mime_type="text/plain")
    assert exc.value.error_code is ErrorCode.NOT_FOUND


def test_upload_route_uses_same_path(client) -> None:
    r = client.post("/v1/corpora", json={"name": "c"})
    assert r.status_code == 201, r.text
    corpus_id = r.json()["id"]

    r = client.post(
        f"/v1/corpora/{corpus_id}/documents",
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"external_id": "ext:1"},
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]

    r = client.post(
        f"/v1/corpora/{corpus_id}/documents",
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"external_id": "ext:1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == doc_id
    assert r.json()["revision"] == 1
