"""M11: garbage collection (POST /v1/admin/gc) and response shapes the UI contract relies on."""

from __future__ import annotations

import copy
import os
import time
from typing import Any

from fastapi.testclient import TestClient

from ragcentre.api.openapi import openapi
from ragcentre.context import AppContext
from ragcentre.pipeline.gc import BLOB_GRACE_SECONDS
from ragcentre.pipeline.planner import archive_pipeline, create_pipeline
from ragcentre.queue.claims import claim_task, complete_task
from ragcentre.queue.tasks import run_task
from test_m6_ingestion import _add_providers, _corpus, _pipeline, _upload
from test_m7_retrieval import drain


def run_tasks(ctx: AppContext) -> None:
    for _ in range(100):
        task = claim_task(ctx, "test")
        if task is None:
            return
        run_task(ctx, task)
        complete_task(ctx, task["id"], "test", task["attempt"])
    raise AssertionError("tasks did not drain")


def _age_all_blobs(ctx: AppContext) -> None:
    old = time.time() - BLOB_GRACE_SECONDS - 60
    for sha in ctx.blobs.iter_all():
        os.utime(ctx.blobs.path(sha), (old, old))


def _two_pipelines(ctx: AppContext, client: TestClient) -> tuple[str, str, str]:
    _add_providers(ctx)
    cid = _corpus(ctx)
    _upload(client, cid, b"# Title\n\nSome text about warranties.\n\n## More\n\nMore text.", external_id="a")
    keep = create_pipeline(ctx, cid, _pipeline())
    other_cfg = _pipeline()
    other_cfg["name"] = "fixed"
    chunk = next(n for n in other_cfg["nodes"] if n["id"] == "chunk")
    chunk["strategy"] = "fixed"
    chunk["options"] = {"max_chars": 200, "overlap_chars": 0}
    other = create_pipeline(ctx, cid, other_cfg)
    drain(ctx)
    return cid, keep, other


def test_gc_dry_run_then_collect(ctx: AppContext, client: TestClient) -> None:
    cid, keep, other = _two_pipelines(ctx, client)
    # Nothing is garbage while both pipelines are live.
    empty = client.post("/v1/admin/gc").json()
    assert empty == {"dry_run": True, "indexes": [], "node_count": 0, "blob_count": 0, "blob_bytes": 0}

    ctx.db.execute("UPDATE corpora SET active_pipeline_id = ? WHERE id = ?", (keep, cid))
    archive_pipeline(ctx, other)
    preview = client.post("/v1/admin/gc", json={"dry_run": True}).json()
    # The fixed pipeline's own chunk, embed and two index nodes; parse is shared with "keep".
    assert preview["node_count"] == 4
    assert sorted(i["kind"] for i in preview["indexes"]) == ["fts", "vector"]
    assert all(i["corpus_id"] == cid for i in preview["indexes"])
    other_chunks = ctx.db.scalar(
        "SELECT COUNT(*) FROM chunks WHERE chunk_node_fp IN (SELECT node_fp FROM pipeline_nodes "
        "WHERE pipeline_id = ? AND node_id = 'chunk')",
        (other,),
    )
    assert other_chunks > 0  # the dry run removed nothing

    report = client.post("/v1/admin/gc", json={"dry_run": False}).json()
    assert report["dry_run"] is False and report["node_count"] == 4
    run_tasks(ctx)  # drop_index tasks
    assert ctx.db.scalar("SELECT COUNT(*) FROM indexes WHERE corpus_id = ?", (cid,)) == 2
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM node_runs WHERE node_fp IN (SELECT node_fp FROM pipeline_nodes WHERE pipeline_id = ?) "
            "AND node_fp NOT IN (SELECT node_fp FROM pipeline_nodes WHERE pipeline_id = ?)",
            (other, keep),
        )
        == 0
    )
    # The kept pipeline still answers queries; the archived one still describes itself.
    r = client.post(f"/v1/corpora/{cid}/query", json={"query": "warranties"})
    assert r.status_code == 200 and r.json()["results"]
    assert client.get(f"/v1/pipelines/{other}").json()["archived"] is True
    again = client.post("/v1/admin/gc").json()
    assert again["node_count"] == 0 and again["indexes"] == []


def test_gc_blobs_respect_references_and_grace(ctx: AppContext, client: TestClient) -> None:
    cid, _, _ = _two_pipelines(ctx, client)
    orphan = ctx.blobs.put(b"nobody references this")
    assert client.post("/v1/admin/gc").json()["blob_count"] == 0  # too new
    _age_all_blobs(ctx)
    preview = client.post("/v1/admin/gc").json()
    assert preview["blob_count"] == 1 and preview["blob_bytes"] == len(b"nobody references this")
    # Re-using an old orphan (an upload of the same bytes) refreshes it, so it is safe again.
    ctx.blobs.put(b"nobody references this")
    assert client.post("/v1/admin/gc").json()["blob_count"] == 0
    _age_all_blobs(ctx)
    client.post("/v1/admin/gc", json={"dry_run": False})
    assert not ctx.blobs.exists(orphan)
    doc = ctx.db.one("SELECT content_sha256 FROM documents WHERE corpus_id = ?", (cid,))
    assert ctx.blobs.exists(doc["content_sha256"])  # referenced blobs are never collected
    assert ctx.db.scalar("SELECT COUNT(*) FROM parse_cache") > 0
    assert all(ctx.blobs.exists(r[0]) for r in ctx.db.all("SELECT blob_sha256 FROM parse_cache"))


def test_gc_skips_nodes_with_running_runs(ctx: AppContext, client: TestClient) -> None:
    cid, keep, other = _two_pipelines(ctx, client)
    ctx.db.execute("UPDATE corpora SET active_pipeline_id = ? WHERE id = ?", (keep, cid))
    archive_pipeline(ctx, other)
    fp = ctx.db.scalar("SELECT node_fp FROM pipeline_nodes WHERE pipeline_id = ? AND node_id = 'chunk'", (other,))
    ctx.db.execute("UPDATE node_runs SET status = 'running' WHERE node_fp = ?", (fp,))
    assert client.post("/v1/admin/gc").json()["node_count"] == 3


def test_pipeline_config_response_omits_unset_settings(ctx: AppContext, client: TestClient) -> None:
    _add_providers(ctx)
    cid = _corpus(ctx)
    cfg: dict[str, Any] = copy.deepcopy(_pipeline())
    cfg["retrieval"]["fusion"] = {"k": 30}  # method omitted: it defaults to rrf
    r = client.post(f"/v1/corpora/{cid}/pipelines", json=cfg)
    assert r.status_code == 201, r.text
    config = r.json()["config"]

    def no_nulls(value: Any) -> bool:
        if isinstance(value, dict):
            return all(v is not None and no_nulls(v) for v in value.values())
        if isinstance(value, list):
            return all(no_nulls(v) for v in value)
        return True

    assert no_nulls(config)
    assert config["retrieval"]["fusion"] == {"method": "rrf", "k": 30}
    chunk = next(n for n in config["nodes"] if n["id"] == "chunk")
    assert "provider" not in chunk and chunk["inputs"] == ["parse"]
    assert client.get(f"/v1/pipelines/{r.json()['id']}").json()["config"] == config


def test_openapi_documents_every_json_response() -> None:
    doc = openapi()
    schemas = doc["components"]["schemas"]
    # Response models mark every field required, so the generated UI types match the contract.
    for name in ("Corpus", "Pipeline", "EvalRunDetail", "Document", "GcReport", "MetadataSchema-Output"):
        props = set(schemas[name]["properties"])
        assert set(schemas[name].get("required", [])) == props, name
    upload = doc["paths"]["/v1/corpora/{corpus_id}/documents"]["post"]["responses"]
    assert upload["201"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Document"}
    create = doc["paths"]["/v1/corpora/{corpus_id}/pipelines"]["post"]["responses"]
    assert create["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/PipelinePlan")
    assert create["201"]["content"]["application/json"]["schema"]["$ref"].endswith("/Pipeline")


def test_rename_corpus(client: TestClient) -> None:
    a = client.post("/v1/corpora", json={"name": "alpha"}).json()
    client.post("/v1/corpora", json={"name": "beta"})
    r = client.patch(f"/v1/corpora/{a['id']}", json={"name": "  gamma "})
    assert r.status_code == 200 and r.json()["name"] == "gamma" and r.json()["id"] == a["id"]
    assert client.patch(f"/v1/corpora/{a['id']}", json={"name": "beta"}).status_code == 409
    assert client.patch(f"/v1/corpora/{a['id']}", json={"name": " "}).json()["error"]["code"] == "validation_error"
    assert client.patch("/v1/corpora/nope", json={"name": "x"}).status_code == 404


def test_document_list_filters(ctx: AppContext, client: TestClient) -> None:
    cid, keep, other = _two_pipelines(ctx, client)
    _upload(client, cid, b"# Second\n\nAnother document.", filename="Billing-Guide.md", external_id="billing-x")
    names = lambda r: sorted(d["filename"] for d in r.json()["items"])  # noqa: E731
    assert names(client.get(f"/v1/corpora/{cid}/documents", params={"q": "billing"})) == ["Billing-Guide.md"]
    assert names(client.get(f"/v1/corpora/{cid}/documents", params={"q": "BILLING-X"})) == ["Billing-Guide.md"]
    # The new upload has pending runs; the first document is fully built.
    assert names(client.get(f"/v1/corpora/{cid}/documents", params={"state": "pending"})) == ["Billing-Guide.md"]
    assert names(client.get(f"/v1/corpora/{cid}/documents", params={"state": "succeeded"})) == ["a.md"]
    drain(ctx)
    fp = ctx.db.scalar("SELECT node_fp FROM pipeline_nodes WHERE pipeline_id = ? AND node_id = 'embed'", (other,))
    doc = ctx.db.scalar("SELECT id FROM documents WHERE filename = 'a.md'")
    ctx.db.execute("UPDATE node_runs SET status = 'failed' WHERE node_fp = ? AND document_id = ?", (fp, doc))
    assert names(client.get(f"/v1/corpora/{cid}/documents", params={"state": "failed"})) == ["a.md"]
    # Restricted to the pipeline that does not use the failed node, a.md is fine.
    r = client.get(f"/v1/corpora/{cid}/documents", params={"state": "failed", "pipeline_id": keep})
    assert names(r) == []
    assert client.get(f"/v1/corpora/{cid}/documents", params={"state": "bogus"}).status_code == 422
    assert client.get(f"/v1/corpora/{cid}/documents", params={"pipeline_id": "nope"}).status_code == 404


def test_diff_reports_retrieval_settings_only_b_has(ctx: AppContext, client: TestClient) -> None:
    _add_providers(ctx)
    cid = _corpus(ctx)
    a = create_pipeline(ctx, cid, _pipeline())
    cfg = _pipeline()
    cfg["name"] = "expanded"
    cfg["retrieval"]["expand"] = {"neighbors": 1}
    b = create_pipeline(ctx, cid, cfg)
    d = client.get(f"/v1/pipelines/{a}/diff", params={"against": b}).json()
    assert d["retrieval_same"] is False
    assert d["retrieval_changes"] == [{"path": "expand", "a": None, "b": {"neighbors": 1}}]
    assert all(n["same"] for n in d["nodes"])
