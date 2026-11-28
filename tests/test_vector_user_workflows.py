from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ragcentre.api.app import create_app
from ragcentre.context import open_context
from ragcentre.settings import Settings
from vector_workflow_helpers import (
    METADATA,
    PROFILES,
    TEXTS,
    Profile,
    Workflow,
    body,
    create_store,
    eventually,
    opened_workflow,
    pipeline_config,
    profile_endpoint,
    wait_ready,
)


@pytest.fixture
def workflow(profile: Profile, tmp_path: Path) -> Iterator[Workflow]:
    with profile_endpoint(profile, tmp_path) as endpoint:
        settings = Settings(
            data_dir=tmp_path / "application",
            workers=0,
            secret_key=Fernet.generate_key().decode(),
            egress_allow=("127.0.0.1", "localhost", "::1"),
            discovery_urls="",
            idle_sleep_seconds=0.01,
            max_idle_sleep_seconds=0.05,
        )
        ctx = open_context(settings)
        try:
            with TestClient(create_app(ctx)) as client:
                yield from opened_workflow(profile, endpoint, ctx, client)
        finally:
            ctx.close()


def assert_documents(result: dict[str, Any], expected: set[str]) -> None:
    assert result["partial"] is False, result
    rows = result["results"]
    assert {row["document_id"] for row in rows} == expected, result
    assert len(rows) == len(expected), result
    assert len({row["chunk_id"] for row in rows}) == len(rows), result
    assert all(set(row["sources"]) == {"dense"} for row in rows), result


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_vector_document_lifecycle(workflow: Workflow) -> None:
    w = workflow
    corpus = w.corpus()
    documents = {key: w.upload(corpus, key) for key in TEXTS}
    pipeline = w.build(corpus)
    expected = {doc["id"] for doc in documents.values()}

    def visible() -> dict[str, Any]:
        result = w.query(corpus, pipeline)
        assert_documents(result, expected)
        return result

    result = eventually(visible)
    assert result["pipeline_id"] == pipeline
    for row in result["results"]:
        key = next(key for key, doc in documents.items() if doc["id"] == row["document_id"])
        assert row["text"] == TEXTS[key]
        assert row["document"]["metadata"] == METADATA[key]
        chunks = body(w.client.get(f"/v1/documents/{row['document_id']}/chunks", params={"pipeline_id": pipeline}))
        assert row["chunk_id"] in {chunk["id"] for chunk in chunks}
    chunk_ids = {row["chunk_id"] for row in result["results"]}

    def stored() -> None:
        assert w.stored_ids(pipeline) == chunk_ids

    eventually(stored)

    cases = [
        ({"group": "fruit"}, {"apricot"}),
        ({"group": {"$ne": "fruit"}}, {"saffron", "cobalt"}),
        ({"group": {"$in": ["fruit", "spice"]}}, {"apricot", "saffron"}),
        ({"group": {"$nin": ["fruit", "spice"]}}, {"cobalt"}),
        ({"year": {"$gte": 2025}}, {"apricot", "saffron"}),
        ({"rating": {"$lt": 3.0}}, {"cobalt"}),
        ({"published": False}, {"cobalt"}),
        ({"$and": [{"published": True}, {"$or": [{"year": 2026}, {"rating": {"$lte": 1.5}}]}]}, {"apricot"}),
        ({"group": "absent"}, set()),
    ]
    for flt, keys in cases:
        wanted = {documents[key]["id"] for key in keys}
        filtered = w.query(corpus, pipeline, filter=flt)
        assert_documents(filtered, wanted)
        assert w.stored_ids(pipeline, filter=flt) == {row["chunk_id"] for row in filtered["results"]}, flt

    w.ctx.close_workers()
    w.ctx.providers.close()
    reopened = open_context(w.ctx.settings)
    try:
        with TestClient(create_app(reopened)) as client:
            persisted = body(
                client.post(
                    f"/v1/corpora/{corpus}/query",
                    json={
                        "query": TEXTS["apricot"],
                        "pipeline_id": pipeline,
                    },
                )
            )
            assert_documents(persisted, expected)
            assert {row["chunk_id"] for row in persisted["results"]} == chunk_ids
    finally:
        reopened.close()
    w.ctx.start_workers(2)

    apricot = documents["apricot"]
    replacement = "REPLACED_MARKER Apricot preserves are sealed in clean glass jars."
    updated = w.upload(corpus, "apricot", text=replacement, status=200)
    assert updated["id"] == apricot["id"]
    assert updated["revision"] == apricot["revision"] + 1
    wait_ready(w.client, pipeline)

    def replaced() -> dict[str, Any]:
        current = w.query(corpus, pipeline, query=replacement)
        assert_documents(current, expected)
        rows = [row for row in current["results"] if row["document_id"] == apricot["id"]]
        assert [row["text"] for row in rows] == [replacement]
        assert all("APRICOT_MARKER" not in row["text"] for row in current["results"])
        assert rows[0]["sources"]["dense"]["score"] == pytest.approx(1.0, abs=0.02)
        assert len(w.stored_ids(pipeline)) == 3
        return current

    eventually(replaced)

    w.ctx.close_workers()
    patched = METADATA["apricot"] | {"group": "preserves", "year": 2027, "published": False}
    assert body(w.client.patch(f"/v1/documents/{apricot['id']}", json={"metadata": patched}))["metadata"] == patched
    pending = w.query(corpus, pipeline, filter={"group": "fruit"}, allow_partial=True)
    assert pending["partial"] is True
    assert pending["results"] == []
    w.ctx.start_workers(2)
    wait_ready(w.client, pipeline)

    def refreshed() -> None:
        result = w.query(corpus, pipeline, filter={"group": "preserves"})
        assert_documents(result, {apricot["id"]})
        assert result["results"][0]["document"]["metadata"] == patched
        assert w.stored_ids(pipeline, filter={"group": "preserves"}) == {result["results"][0]["chunk_id"]}
        assert w.stored_ids(pipeline, filter={"group": "fruit"}) == set()

    eventually(refreshed)

    second = w.corpus("isolation")
    isolated_doc = w.upload(second, "apricot")
    isolated_pipeline = w.build(second)

    def isolated() -> None:
        assert_documents(w.query(second, isolated_pipeline), {isolated_doc["id"]})

    eventually(isolated)
    assert_documents(w.query(corpus, pipeline), expected)
    assert w.index(pipeline)["physical_name"] != w.index(isolated_pipeline)["physical_name"]

    cobalt = documents["cobalt"]
    cobalt_chunks = {row["chunk_id"] for row in result["results"] if row["document_id"] == cobalt["id"]}
    body(w.client.delete(f"/v1/documents/{cobalt['id']}"), 202)

    def deleted() -> None:
        remaining = w.query(corpus, pipeline, allow_partial=True)
        assert {row["document_id"] for row in remaining["results"]} == expected - {cobalt["id"]}
        assert not cobalt_chunks & w.stored_ids(pipeline)
        assert not w.stored_ids(pipeline, filter={"document_id": cobalt["id"]})

    eventually(deleted)
    assert_documents(w.query(second, isolated_pipeline), {isolated_doc["id"]})
    w.check_sentinel()


MUTABLE = {
    "qdrant": {"hnsw_ef": 77},
    "pgvector": {"ef_search": 77, "max_scan_tuples": 12345},
    "chroma": {"ef_search": 77},
    "lancedb": {"nprobes": 3},
    "redis": {"ef_runtime": 77},
    "weaviate": {"hnsw_ef": 77},
    "elasticsearch": {"num_candidates_factor": 17},
    "opensearch": {"num_candidates_factor": 17},
    "milvus": {"hnsw_ef": 77},
    "mongodb": {"exact": False, "num_candidates_factor": 17},
}
IMMUTABLE = {
    "qdrant": {"hnsw_m": 24, "hnsw_ef_construction": 123, "quantization": "scalar"},
    "pgvector": {"vector_type": "halfvec", "hnsw_m": 24, "hnsw_ef_construction": 123},
    "chroma": {"hnsw_max_neighbors": 24, "hnsw_ef_construction": 123},
    "lancedb": {"ann_index_min_rows": 256, "index_type": "IVF_SQ"},
    "redis": {"algorithm": "hnsw"},
    "weaviate": {"hnsw_max_connections": 24, "hnsw_ef_construction": 123},
    "elasticsearch": {"index_options": "int8_hnsw"},
    "opensearch": {"engine": "lucene"},
    "milvus": {"index_type": "HNSW", "hnsw_m": 24, "hnsw_ef_construction": 123},
}


def observe_search(w: Workflow, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    store = w.ctx.providers.store(w.provider["id"])
    adapter = w.profile.adapter
    calls: list[tuple[Any, Any]] = []
    if adapter in {"qdrant", "redis", "elasticsearch", "opensearch", "milvus"}:
        client = store._client
        method = {
            "qdrant": "query_points",
            "redis": "execute_command",
            "elasticsearch": "search",
            "opensearch": "search",
            "milvus": "search",
        }[adapter]
        original = getattr(client, method)

        def observed(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        monkeypatch.setattr(client, method, observed)
    elif adapter == "mongodb":
        collection_type = type(store._db["unused_observer"])
        original = collection_type.aggregate

        def aggregate(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(collection_type, "aggregate", aggregate)
    elif adapter == "pgvector":
        import psycopg

        original = psycopg.Connection.execute

        def execute(self: Any, statement: Any, *args: Any, **kwargs: Any) -> Any:
            sql = statement if isinstance(statement, str) else statement.as_string(self)
            calls.append((sql, kwargs))
            return original(self, statement, *args, **kwargs)

        monkeypatch.setattr(psycopg.Connection, "execute", execute)
    return calls


def assert_effective_search(w: Workflow, pipeline: str, calls: list[tuple[Any, Any]], edits: dict[str, Any]) -> None:
    store = w.ctx.providers.store(w.provider["id"])
    adapter = w.profile.adapter
    name = w.index(pipeline)["physical_name"]
    if adapter == "qdrant":
        assert any(
            kwargs.get("search_params") is not None and kwargs["search_params"].hnsw_ef == 77 for _, kwargs in calls
        ), calls
    elif adapter == "chroma":
        native = store._client.get_collection(w.prefix + name)
        assert native.configuration["hnsw"]["ef_search"] == 77
    elif adapter == "weaviate":
        from ragcentre.stores.weaviate import physical_name

        config = store._client.collections.get(physical_name(w.prefix, name)).config.get()
        assert config.vector_config["default"].vector_index_config.ef == 77
    elif adapter == "redis":
        assert any("EF" in args and args[args.index("EF") + 1] == 77 for args, _ in calls), calls
    elif adapter == "pgvector":
        statements = [sql for sql, _ in calls]
        for field, value in edits.items():
            assert f"SET LOCAL hnsw.{field} = {value}" in statements, statements
    elif adapter == "elasticsearch":
        assert any(kwargs.get("knn", {}).get("num_candidates") == 170 for _, kwargs in calls), calls
    elif adapter == "opensearch":
        assert any(
            kwargs.get("body", {})
            .get("query", {})
            .get("knn", {})
            .get("_rc_vector", {})
            .get("method_parameters", {})
            .get("ef_search")
            == 170
            for _, kwargs in calls
        ), calls
    elif adapter == "milvus":
        assert any(kwargs.get("search_params", {}).get("params", {}).get("ef") == 77 for _, kwargs in calls), calls
    elif adapter == "mongodb":
        searches = [
            stage["$vectorSearch"]
            for args, _ in calls
            if args
            for stage in args[0]
            if stage.get("$vectorSearch", {}).get("limit") == 10
        ]
        assert searches, calls
        if edits["exact"]:
            assert all(search.get("exact") is True and "numCandidates" not in search for search in searches), searches
        else:
            assert all(search.get("numCandidates") == 170 for search in searches), searches


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_provider_strategy_edits_reach_real_store(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch, record_property: Any
) -> None:
    w = workflow
    corpus = w.corpus()
    doc = w.upload(corpus, "apricot")
    pipeline = w.build(corpus)

    def visible() -> None:
        assert_documents(w.query(corpus, pipeline), {doc["id"]})

    eventually(visible)
    index_before = w.index(pipeline)
    ready_before = body(w.client.get(f"/v1/pipelines/{pipeline}/status"))
    provider = body(w.client.get(f"/v1/providers/{w.provider['id']}"))
    immutable = IMMUTABLE.get(w.profile.adapter, {})
    for key, alternate in immutable.items():
        if provider["config"][key] == alternate:
            alternate = {
                "vector_type": "vector",
                "algorithm": "flat",
                "index_options": "hnsw",
                "engine": "faiss",
                "index_type": "FLAT",
            }.get(key, alternate)
        response = w.client.patch(f"/v1/providers/{provider['id']}", json={"config": {key: alternate}})
        assert response.status_code == 409, (key, response.text)
        assert response.json()["error"]["code"] == "immutable_field", response.text
    edits = dict(MUTABLE.get(w.profile.adapter, {}))
    if w.profile.adapter == "redis" and provider["config"]["algorithm"] == "flat":
        response = w.client.patch(f"/v1/providers/{provider['id']}", json={"config": edits})
        assert response.status_code == 422, response.text
        edits = {}
    if w.profile.adapter == "milvus" and provider["config"]["index_type"] == "FLAT":
        edits = {}
        record_property("strategy_limitation", "FLAT has no HNSW search effort")
    if w.profile.adapter == "opensearch" and provider["config"]["engine"] == "lucene":
        edits = {}
        record_property("strategy_limitation", "Lucene search effort is not exposed by this adapter")
    if w.profile.adapter == "mongodb":
        edits["exact"] = not provider["config"]["exact"]
    if w.profile.id == "qdrant_embedded":
        record_property("strategy_limitation", "Embedded Qdrant is exact; hnsw_ef is transmitted but has no effect")
    if edits:
        for key, value in edits.items():
            if not isinstance(value, bool):
                invalid = w.client.patch(f"/v1/providers/{provider['id']}", json={"config": {key: -2}})
                assert invalid.status_code == 422, (key, invalid.text)
        changed = body(w.client.patch(f"/v1/providers/{provider['id']}", json={"config": edits}))
        assert changed["fingerprint"] == provider["fingerprint"]
        assert changed["secrets"] == provider["secrets"], "Search tuning must preserve database credentials"
        reloaded = body(w.client.get(f"/v1/providers/{provider['id']}"))
        assert all(reloaded["config"][key] == value for key, value in edits.items())
        with monkeypatch.context() as observer:
            calls = observe_search(w, observer)
            visible()
            assert_effective_search(w, pipeline, calls, edits)
        if w.profile.adapter == "lancedb":
            record_property("strategy_limitation", "nprobes is unused below the ANN threshold; this profile is exact")
        assert w.index(pipeline) == index_before
        assert body(w.client.get(f"/v1/pipelines/{pipeline}/status")) == ready_before
    w.check_sentinel()


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_switch_active_vector_database_reuses_upstream(workflow: Workflow) -> None:
    w = workflow
    corpus = w.corpus()
    doc = w.upload(corpus, "apricot")
    create_store(w.client, "sqlite_vec", "workflow-baseline", {"path": "baseline.sqlite", "prefix": w.prefix})
    baseline = w.build(corpus, store="workflow-baseline", name="baseline")
    config = pipeline_config(w.provider["name"], name="candidate")
    plan = body(w.client.post(f"/v1/corpora/{corpus}/pipelines", params={"dry_run": "true"}, json=config))
    assert plan["valid"], plan
    nodes = {node["node_id"]: node for node in plan["nodes"]}
    assert all(nodes[key]["reused"] for key in ("parse", "chunk", "embed")), plan
    assert nodes["vec"]["reused"] is False, plan
    candidate = w.build(corpus, name="candidate")
    for pipeline in (baseline, candidate):

        def visible(current_pipeline: str = pipeline) -> dict[str, Any]:
            result = w.query(corpus, current_pipeline)
            assert_documents(result, {doc["id"]})
            return result

        result = eventually(visible)
        assert w.stored_ids(pipeline) == {row["chunk_id"] for row in result["results"]}
        body(w.client.put(f"/v1/corpora/{corpus}/active-pipeline", json={"pipeline_id": pipeline}))
        active = w.query(corpus, None)
        assert active["pipeline_id"] == pipeline
        assert active["pipeline_fingerprint"] == result["pipeline_fingerprint"]
        assert_documents(active, {doc["id"]})
    assert w.index(baseline)["provider_id"] != w.index(candidate)["provider_id"]
    body(w.client.put(f"/v1/corpora/{corpus}/active-pipeline", json={"pipeline_id": baseline}))
    assert w.query(corpus, None)["pipeline_id"] == baseline
    w.check_sentinel()
