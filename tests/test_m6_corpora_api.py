from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.pipeline.planner import create_pipeline
from test_m6_ingestion import _add_providers, _pipeline
from test_m7_retrieval import drain

SCHEMA = {"fields": {"lang": {"type": "str", "default": "en"}}}


def _create(client: TestClient, name: str = "docs") -> dict[str, object]:
    r = client.post("/v1/corpora", json={"name": name, "metadata_schema": SCHEMA})
    assert r.status_code == 201, r.text
    body: dict[str, object] = r.json()
    return body


def test_corpus_create_shape_and_conflicts(client: TestClient) -> None:
    c = _create(client)
    assert set(c) == {
        "id",
        "name",
        "metadata_schema",
        "active_pipeline_id",
        "document_count",
        "pipelines",
        "team_id",
        "created_at",
    }
    assert client.post("/v1/corpora", json={"name": "docs"}).status_code == 409
    bad = {"name": "x", "metadata_schema": {"fields": {"year": {"type": "int", "default": "2020"}}}}
    assert client.post("/v1/corpora", json=bad).status_code == 422
    reserved = {"name": "y", "metadata_schema": {"fields": {"document_id": {"type": "str", "default": ""}}}}
    assert client.post("/v1/corpora", json=reserved).status_code == 422


def test_schema_is_additive_only(ctx: AppContext, client: TestClient) -> None:
    _add_providers(ctx)
    c = _create(client)
    cid = str(c["id"])
    client.post(f"/v1/corpora/{cid}/documents", files={"file": ("a.md", b"# A\n\ntext", "text/markdown")},
                data={"metadata": "{}"})  # fmt: skip
    create_pipeline(ctx, cid, _pipeline())
    drain(ctx)
    added = {"fields": {**SCHEMA["fields"], "year": {"type": "int", "default": 2024}}}
    r = client.patch(f"/v1/corpora/{cid}/metadata-schema", json=added)
    assert r.status_code == 200, r.text
    assert r.json()["metadata_schema"] == added
    # Adding a field resets index_vector runs so records are rewritten with the new field.
    pending = ctx.db.scalar(
        "SELECT COUNT(*) FROM node_runs r JOIN nodes n ON n.fingerprint = r.node_fp "
        "WHERE n.type = 'index_vector' AND r.status = 'pending'"
    )
    assert pending == 1
    drain(ctx)
    retyped = {"fields": {**added["fields"], "year": {"type": "str", "default": "x"}}}
    assert client.patch(f"/v1/corpora/{cid}/metadata-schema", json=retyped).json()["error"]["code"] == "immutable_field"
    assert client.patch(f"/v1/corpora/{cid}/metadata-schema", json=SCHEMA).status_code == 409  # removal


def test_document_metadata_types_and_content_replace(ctx: AppContext, client: TestClient) -> None:
    c = _create(client)
    cid = str(c["id"])
    files = {"file": ("a.md", b"# A\n\ntext", "text/markdown")}
    bad = client.post(f"/v1/corpora/{cid}/documents", files=files, data={"metadata": json.dumps({"lang": 3})})
    assert bad.status_code == 422
    doc = client.post(f"/v1/corpora/{cid}/documents", files=files, data={"metadata": json.dumps({"lang": "de"})}).json()
    assert client.patch(f"/v1/documents/{doc['id']}", json={"metadata": {"lang": True}}).status_code == 422
    r = client.put(f"/v1/documents/{doc['id']}/content", files={"file": ("b.md", b"# B\n\nnew", "text/markdown")})
    assert r.status_code == 200 and r.json()["revision"] == 2 and r.json()["filename"] == "b.md"
    assert client.get(f"/v1/documents/{doc['id']}/content").content == b"# B\n\nnew"
