from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.pipeline.planner import create_pipeline, plan, resolve_for_corpus
from ragcentre.pipeline.status import status

EMB = "emb"


def _add_providers(ctx: AppContext) -> None:
    ctx.providers.create("hash", "embedder", "builtin_hash", {})
    ctx.providers.create("store", "vector_store", "sqlite_vec", {})
    ctx.providers.create("docling", "parser", "http", {"base_url": "http://parser.test"})


def _corpus(ctx: AppContext, name: str = "corpus") -> str:
    from ragcentre.hashing import new_id
    from ragcentre.timeutil import now_iso

    cid = new_id()
    ctx.db.execute(
        "INSERT INTO corpora (id, name, metadata_schema, created_at) VALUES (?, ?, '{}', ?)", (cid, name, now_iso())
    )
    return cid


def _base_pipeline(chunk: str = "structure") -> dict[str, Any]:
    return {
        "name": "base",
        "nodes": [
            {
                "id": "parse",
                "type": "parse",
                "routes": [{"mime": "text/*", "provider": "builtin-text"}, {"mime": "*/*", "provider": "docling"}],
            },
            {"id": "chunk", "type": "chunk", "inputs": ["parse"], "strategy": chunk, "options": {"max_chars": 800}},
            {"id": "embed", "type": "embed", "inputs": ["chunk"], "provider": "hash"},
            {
                "id": "vec",
                "type": "index_vector",
                "inputs": ["embed"],
                "provider": "store",
                "options": {"metric": "cosine"},
            },
            {"id": "fts", "type": "index_fts", "inputs": ["chunk"], "options": {"tokenizer": "unicode61"}},
        ],
        "retrieval": {
            "sources": [{"id": "dense", "index": "vec", "top_k": 30}, {"id": "sparse", "index": "fts", "top_k": 30}],
            "fusion": {"method": "rrf", "k": 60},
            "top_k": 8,
        },
    }


def _doc(ctx: AppContext, corpus_id: str, doc_id: str = "d1") -> None:
    ctx.db.execute(
        "INSERT INTO documents (id, corpus_id, filename, mime_type, size_bytes, content_sha256, metadata, "
        "attributes, revision, status, created_at, updated_at) VALUES (?, ?, 'a.md', 'text/markdown', 10, 'x', "
        "'{}', '{}', 1, 'active', 't', 't')",
        (doc_id, corpus_id),
    )


def _runs(ctx: AppContext, corpus_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in ctx.db.all("SELECT * FROM node_runs WHERE corpus_id = ? ORDER BY node_fp", (corpus_id,))]


def _complete(ctx: AppContext, corpus_id: str, doc_id: str | None = None) -> None:
    sql = "UPDATE node_runs SET status = 'succeeded' WHERE corpus_id = ?"
    args: list[Any] = [corpus_id]
    if doc_id:
        sql += " AND document_id = ?"
        args.append(doc_id)
    ctx.db.execute(sql, args)


def test_two_pipelines_share_parse_and_differ_downstream(ctx: AppContext) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    _doc(ctx, corpus_id)
    create_pipeline(ctx, corpus_id, _base_pipeline())
    cfg2 = _base_pipeline()
    cfg2["name"] = "changed-chunk"
    cfg2["nodes"][1]["options"]["max_chars"] = 500
    create_pipeline(ctx, corpus_id, cfg2)
    resolved = [json.loads(r["resolved"]) for r in ctx.db.all("SELECT resolved FROM pipelines ORDER BY created_at")]
    nodes1 = {n["node_id"]: n for n in resolved[0]["nodes"]}
    nodes2 = {n["node_id"]: n for n in resolved[1]["nodes"]}
    assert nodes1["parse"]["node_fp"] == nodes2["parse"]["node_fp"]
    for name in ("chunk", "embed", "vec", "fts"):
        assert nodes1[name]["node_fp"] != nodes2[name]["node_fp"], name
    runs = _runs(ctx, corpus_id)
    # Shared parse has one run; everything else has two.
    assert len([r for r in runs if r["node_fp"] == nodes1["parse"]["node_fp"]]) == 1
    assert len(runs) == 1 + 4 * 2


def test_retrieval_only_change_creates_zero_runs(ctx: AppContext) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    _doc(ctx, corpus_id)
    create_pipeline(ctx, corpus_id, _base_pipeline())
    cfg2 = _base_pipeline()
    cfg2["name"] = "new-k"
    cfg2["retrieval"]["top_k"] = 4
    before = len(_runs(ctx, corpus_id))
    pid2 = create_pipeline(ctx, corpus_id, cfg2)
    assert len(_runs(ctx, corpus_id)) == before
    resolved = [json.loads(r["resolved"]) for r in ctx.db.all("SELECT resolved FROM pipelines ORDER BY created_at")]
    assert {n["node_fp"] for n in resolved[0]["nodes"]} == {n["node_fp"] for n in resolved[1]["nodes"]}
    assert resolved[0]["retrieval"] != resolved[1]["retrieval"]
    assert json.loads(ctx.db.scalar("SELECT resolved FROM pipelines WHERE id = ?", (pid2,)))["fingerprint"] != json.loads(ctx.db.scalar("SELECT resolved FROM pipelines WHERE id = ?", (ctx.db.scalar("SELECT id FROM pipelines WHERE corpus_id = ? ORDER BY created_at", (corpus_id,)),)))["fingerprint"]  # fmt: skip


def test_node_id_renames_do_not_change_fingerprints(ctx: AppContext) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    _doc(ctx, corpus_id)
    cfg1 = _base_pipeline()
    create_pipeline(ctx, corpus_id, cfg1)
    cfg2 = _base_pipeline()
    cfg2["name"] = "renamed"
    cfg2["nodes"][1]["id"] = "chopper"
    cfg2["nodes"][2]["inputs"] = ["chopper"]
    cfg2["nodes"][3]["inputs"] = ["embed"]
    cfg2["nodes"][4]["inputs"] = ["chopper"]
    create_pipeline(ctx, corpus_id, cfg2)
    r1 = json.loads(ctx.db.scalar("SELECT resolved FROM pipelines WHERE name = 'base'"))
    r2 = json.loads(ctx.db.scalar("SELECT resolved FROM pipelines WHERE name = 'renamed'"))
    assert r1["fingerprint"] == r2["fingerprint"]
    assert [n["node_fp"] for n in r1["nodes"]] == [n["node_fp"] for n in r2["nodes"]]


def test_dry_run_plan_reports_reuse_and_counts(ctx: AppContext, client: TestClient) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    _doc(ctx, corpus_id)
    _doc(ctx, corpus_id, "d2")
    create_pipeline(ctx, corpus_id, _base_pipeline())
    cfg2 = _base_pipeline()
    cfg2["name"] = "second"
    cfg2["nodes"][1]["options"]["max_chars"] = 500
    plan_data = plan(ctx, corpus_id, cfg2)
    assert plan_data["valid"] is True and plan_data["total_runs_to_create"] == 8  # 4 changed nodes x 2 docs
    by_id = {n["node_id"]: n for n in plan_data["nodes"]}
    assert by_id["parse"]["reused"] is True and by_id["parse"]["runs_to_create"] == 0
    assert by_id["chunk"]["reused"] is False and by_id["chunk"]["runs_to_create"] == 2
    assert by_id["parse"]["shared_with"]
    # The dry-run endpoint is always 200, even invalid, and saves nothing.
    before = len(ctx.db.all("SELECT id FROM pipelines"))
    bad = _base_pipeline()
    bad["retrieval"]["top_k"] = 999
    r = client.post(f"/v1/corpora/{corpus_id}/pipelines", params={"dry_run": "true"}, json=bad)
    assert r.status_code == 200
    assert r.json()["valid"] is False and r.json()["problems"]
    assert len(ctx.db.all("SELECT id FROM pipelines")) == before
    # A real create that fails validation returns 422 with details.problems.
    r = client.post(f"/v1/corpora/{corpus_id}/pipelines", json=bad)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_pipeline"
    assert r.json()["error"]["details"]["problems"]


def _expect_problem(problems: list[dict[str, Any]], path: str, node_id: str | None = None) -> None:
    assert any(p["path"] == path and (node_id is None or p["node_id"] == node_id) for p in problems), (
        path,
        node_id,
        problems,
    )


def test_all_twelve_validation_rules(ctx: AppContext) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)

    def problems(cfg: dict[str, Any]) -> list[dict[str, Any]]:
        res = resolve_for_corpus(ctx, corpus_id, cfg)
        return [p.to_json() for p in res.problems]

    def mutate(node_index: int, **changes: Any) -> dict[str, Any]:
        cfg = _base_pipeline()
        for key, value in changes.items():
            if isinstance(value, str) and key.startswith("_"):
                cfg[key[1:]][node_index] = value
            else:
                cfg[key][node_index] = value
        return cfg

    # 1. node ids
    cfg = _base_pipeline()
    cfg["nodes"][0]["id"] = "Bad"
    _expect_problem(problems(cfg), "nodes[0].id")
    cfg = _base_pipeline()
    cfg["nodes"][1]["id"] = "parse"  # duplicate
    _expect_problem(problems(cfg), "nodes[1].id", "parse")
    # 2. unknown input + cycle
    cfg = _base_pipeline()
    cfg["nodes"][1]["inputs"] = ["nope"]
    _expect_problem(problems(cfg), "nodes[1].inputs[0]", "chunk")
    cfg = _base_pipeline()
    cfg["nodes"][1]["inputs"] = ["vec"]  # cycle
    assert any("cycle" in p["message"] for p in problems(cfg))
    # 3. input type / arity
    cfg = _base_pipeline()
    cfg["nodes"][2]["inputs"] = ["parse"]  # embed from parse
    _expect_problem(problems(cfg), "nodes[2].inputs", "embed")
    cfg = _base_pipeline()
    cfg["nodes"][2]["inputs"] = []
    _expect_problem(problems(cfg), "nodes[2].inputs", "embed")
    cfg = _base_pipeline()
    cfg["nodes"][0]["inputs"] = ["x"]  # parse takes none
    _expect_problem(problems(cfg), "nodes[0].inputs", "parse")
    # 4. exactly one parse
    cfg = _base_pipeline()
    cfg["nodes"].append({"id": "parse2", "type": "parse", "routes": []})
    _expect_problem(problems(cfg), "nodes")
    cfg = _base_pipeline()
    cfg["nodes"] = [n for n in cfg["nodes"] if n["type"] != "parse"]
    _expect_problem(problems(cfg), "nodes")
    # 5. provider existence/kind
    cfg = _base_pipeline()
    cfg["nodes"][2]["provider"] = "missing"
    _expect_problem(problems(cfg), "nodes[2].provider", "embed")
    cfg = _base_pipeline()
    cfg["nodes"][3]["provider"] = "hash"  # wrong kind
    _expect_problem(problems(cfg), "nodes[3].provider", "vec")
    # 6. metric capabilities
    cfg = _base_pipeline()
    cfg["nodes"][3]["options"]["metric"] = "dot"
    _expect_problem(problems(cfg), "nodes[3].options.metric", "vec")
    # 7. at least one index node and reachability
    cfg = _base_pipeline()
    cfg["nodes"] = cfg["nodes"][:3]
    _expect_problem(problems(cfg), "nodes")
    cfg = _base_pipeline()
    cfg["nodes"].append({"id": "dangling", "type": "embed", "inputs": ["chunk"], "provider": "hash"})
    _expect_problem(problems(cfg), "nodes[5]", "dangling")
    # 8. source names an index node / unique ids
    cfg = _base_pipeline()
    cfg["retrieval"]["sources"][0]["index"] = "chunk"
    _expect_problem(problems(cfg), "retrieval.sources[0].index")
    cfg = _base_pipeline()
    cfg["retrieval"]["sources"][1]["id"] = "dense"  # duplicate
    _expect_problem(problems(cfg), "retrieval.sources[1].id")
    # 9. same chunk node
    cfg = _base_pipeline()
    cfg["retrieval"]["sources"][1]["index"] = "vec"  # two vector sources? same chunk -> ok
    assert not any("same chunk" in p["message"] for p in problems(cfg))
    # 10. fusion required/forbidden
    cfg = _base_pipeline()
    cfg["retrieval"].pop("fusion")
    _expect_problem(problems(cfg), "retrieval.fusion")
    cfg = _base_pipeline()
    cfg["retrieval"]["sources"] = cfg["retrieval"]["sources"][:1]
    _expect_problem(problems(cfg), "retrieval.fusion")
    # 11. numeric limits
    cfg = _base_pipeline()
    cfg["retrieval"]["sources"][0]["top_k"] = 0
    _expect_problem(problems(cfg), "retrieval.sources[0].top_k")
    cfg = _base_pipeline()
    cfg["retrieval"]["top_k"] = 31  # > min(source top_k)
    _expect_problem(problems(cfg), "retrieval.top_k")
    cfg = _base_pipeline()
    cfg["retrieval"]["expand"] = {"neighbors": 6}
    _expect_problem(problems(cfg), "retrieval.expand.neighbors")
    cfg = _base_pipeline()
    cfg["retrieval"]["rerank"] = {"provider": "missing", "top_n": 5}
    _expect_problem(problems(cfg), "retrieval.rerank.provider")
    # 12. chunk option validation
    cfg = _base_pipeline()
    cfg["nodes"][1]["options"] = {"max_chars": 99}
    _expect_problem(problems(cfg), "nodes[1].options.max_chars", "chunk")


def test_pipeline_crud_and_diff(ctx: AppContext, client: TestClient) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    _doc(ctx, corpus_id)
    cfg = _base_pipeline()
    r = client.post(f"/v1/corpora/{corpus_id}/pipelines", json=cfg)
    assert r.status_code == 201, r.text
    p1 = r.json()
    assert set(p1) == {"id", "corpus_id", "name", "fingerprint", "archived", "status", "coverage", "created_at",
                       "config", "is_active", "nodes"}  # fmt: skip
    assert p1["status"] == "building" and p1["is_active"] is False
    _complete(ctx, corpus_id)
    p1 = client.get(f"/v1/pipelines/{p1['id']}").json()
    assert p1["status"] == "ready" and p1["coverage"] == 1.0
    r = client.get("/v1/pipelines/{}".format(p1["id"]))
    assert r.json() == p1
    # Dry-run is the create body returned as a plan, not saved
    dry = {**cfg, "name": "preview"}
    r = client.post(f"/v1/corpora/{corpus_id}/pipelines", params={"dry_run": "true"}, json=dry)
    assert r.status_code == 200 and r.json()["valid"] is True and r.json()["pipeline_fingerprint"] == p1["fingerprint"]
    # Archived listing
    assert client.get(f"/v1/corpora/{corpus_id}/pipelines").json()["items"][0]["id"] == p1["id"]
    assert client.get(f"/v1/corpora/{corpus_id}/pipelines", params={"archived": "true"}).json()["items"] == []
    # Clone
    clone = {**cfg, "name": "clone"}
    clone["nodes"][1]["options"]["max_chars"] = 900
    p2 = client.post(f"/v1/corpora/{corpus_id}/pipelines", json=clone).json()
    assert p2["fingerprint"] != p1["fingerprint"]
    # Diff: parse shared, chunk changed
    d = client.get(f"/v1/pipelines/{p1['id']}/diff", params={"against": p2["id"]}).json()
    assert d["a"] == p1["id"] and d["b"] == p2["id"]
    by_id = {n["node_id"]: n for n in d["nodes"]}
    assert by_id["parse"]["same"] is True
    assert by_id["chunk"]["same"] is False and by_id["chunk"]["changes"]
    assert any(
        c["path"] == "options" and c["a"]["max_chars"] == 800 and c["b"]["max_chars"] == 900
        for c in by_id["chunk"]["changes"]
    )
    assert d["retrieval_same"] is True and d["retrieval_changes"] == []
    # Status endpoint
    st = client.get(f"/v1/pipelines/{p1['id']}/status").json()
    assert st["pipeline_id"] == p1["id"] and st["document_count"] == 1 and st["status"] == "ready"
    assert len(st["nodes"]) == 5 and st["nodes"][0]["counts"]["succeeded"] == 1
    # Retry-failed
    assert client.post(f"/v1/pipelines/{p1['id']}/retry-failed").json() == {"retried": 0}
    # Archive
    r = client.post(f"/v1/pipelines/{p2['id']}/archive")
    assert r.status_code == 200 and r.json()["archived"] is True
    assert client.get(f"/v1/corpora/{corpus_id}/pipelines").json()["items"][0]["id"] == p1["id"]
    # Active pipeline guard
    client.put(f"/v1/corpora/{corpus_id}/active-pipeline", json={"pipeline_id": p1["id"]})
    r = client.post(f"/v1/pipelines/{p1['id']}/archive")
    assert r.status_code == 409 and r.json()["error"]["code"] == "pipeline_is_active"


def test_status_derivation(ctx: AppContext) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    _doc(ctx, corpus_id)
    _doc(ctx, corpus_id, "d2")
    pid = create_pipeline(ctx, corpus_id, _base_pipeline())
    ctx.db.execute("UPDATE node_runs SET status = 'succeeded' WHERE corpus_id = ?", (corpus_id,))
    assert status(ctx, pid).status == "ready"
    # One failed run, no pending: degraded. Downstream pending run is blocked.
    ctx.db.execute(
        "UPDATE node_runs SET status = 'failed' WHERE corpus_id = ? AND node_fp IN "
        "(SELECT node_fp FROM pipeline_nodes WHERE pipeline_id = ? AND node_id = 'parse') AND document_id = 'd1'",
        (corpus_id, pid),
    )
    ctx.db.execute(
        "UPDATE node_runs SET status = 'pending' WHERE corpus_id = ? AND node_fp IN "
        "(SELECT node_fp FROM pipeline_nodes WHERE pipeline_id = ? AND node_id = 'chunk') AND document_id = 'd1'",
        (corpus_id, pid),
    )
    # Downstream terminal runs for d1 are no longer covered by a successful chain.
    ctx.db.execute(
        "UPDATE node_runs SET status = 'pending' WHERE corpus_id = ? AND node_fp IN "
        "(SELECT node_fp FROM pipeline_nodes WHERE pipeline_id = ? AND node_id IN ('vec', 'fts')) AND document_id = 'd1'",
        (corpus_id, pid),
    )
    st = status(ctx, pid)
    assert st.status == "degraded"
    parse_node = next(n for n in st.nodes if n.node_id == "parse")
    assert parse_node.counts.failed == 1 and parse_node.counts.succeeded == 1
    chunk_node = next(n for n in st.nodes if n.node_id == "chunk")
    assert chunk_node.counts.blocked == 1
    assert st.coverage == 0.5  # only d2 reaches every terminal node
    # A pending run makes it building again
    ctx.db.execute(
        "UPDATE node_runs SET status = 'pending' WHERE document_id = 'd2' AND node_fp IN "
        "(SELECT node_fp FROM pipeline_nodes WHERE pipeline_id = ? AND node_id = 'vec')",
        (pid,),
    )
    assert status(ctx, pid).status == "building"


def test_node_runs_endpoint(ctx: AppContext, client: TestClient) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    _doc(ctx, corpus_id)
    create_pipeline(ctx, corpus_id, _base_pipeline())
    r = client.get("/v1/node-runs")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 5
    assert all(set(n) == {"id", "corpus_id", "document_id", "document_filename", "node_fp", "node_type",
                          "pipelines", "status", "attempt", "max_attempts", "blocked_by_run_id",
                          "error_code", "error_message", "stats", "run_after", "started_at", "finished_at"} for n in items)  # fmt: skip
    assert all(n["document_filename"] == "a.md" and n["max_attempts"] == 5 for n in items)
    # Retry a succeeded run changes it back to pending (retry endpoint does not check state in the mock)
    first = items[0]
    r = client.post(f"/v1/node-runs/{first['id']}/retry")
    assert r.status_code == 200 and r.json()["status"] == "pending"
