from __future__ import annotations

import importlib.util
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from ragcentre.context import AppContext
from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import CollectionSpec, Record

DIM = 32
TEXTS = {
    "apricot": "APRICOT_MARKER Apricot trees bear orange fruit in the orchard.",
    "saffron": "SAFFRON_MARKER Saffron threads season rice with golden color.",
    "cobalt": "COBALT_MARKER Cobalt pigments give ceramic glazes a blue color.",
}
METADATA = {
    "apricot": {"group": "fruit", "year": 2026, "rating": 4.5, "published": True},
    "saffron": {"group": "spice", "year": 2025, "rating": 3.0, "published": True},
    "cobalt": {"group": "mineral", "year": 2024, "rating": 1.5, "published": False},
}
SCHEMA = {
    "fields": {
        "group": {"type": "str", "default": "unknown"},
        "year": {"type": "int", "default": 2000},
        "rating": {"type": "float", "default": 0.0},
        "published": {"type": "bool", "default": False},
    }
}
FIELD_TYPES = {key: value["type"] for key, value in SCHEMA["fields"].items()} | {"document_id": "str"}


@dataclass(frozen=True)
class Profile:
    id: str
    adapter: str
    module: str
    config: dict[str, Any] = field(default_factory=dict)
    endpoint_env: str | None = None
    endpoint_default: str | None = None
    endpoint_key: str | None = None


PROFILES = (
    Profile("sqlite_file", "sqlite_vec", "sqlite_vec", {"path": "workflow.sqlite"}),
    Profile("qdrant_embedded", "qdrant", "qdrant_client", {"path": "workflow-qdrant"}),
    Profile(
        "qdrant_server",
        "qdrant",
        "qdrant_client",
        endpoint_env="RAGCENTRE_TEST_QDRANT_URL",
        endpoint_default="http://127.0.0.1:56333",
        endpoint_key="url",
    ),
    Profile("chroma_local", "chroma", "chromadb", {"mode": "local", "path": "workflow-chroma"}),
    Profile(
        "chroma_http",
        "chroma",
        "chromadb",
        {"mode": "http"},
        "RAGCENTRE_TEST_CHROMA_URL",
        "http://127.0.0.1:58000",
        "host",
    ),
    Profile("lancedb_exact", "lancedb", "lancedb", {"uri": "workflow-lance", "ann_index_min_rows": None}),
    Profile(
        "pgvector_vector",
        "pgvector",
        "psycopg",
        {"vector_type": "vector"},
        "RAGCENTRE_TEST_PGVECTOR_DSN",
        "postgresql://ragcentre:ragcentre@127.0.0.1:54329/ragcentre",
        "dsn",
    ),
    Profile(
        "pgvector_halfvec",
        "pgvector",
        "psycopg",
        {"vector_type": "halfvec"},
        "RAGCENTRE_TEST_PGVECTOR_DSN",
        "postgresql://ragcentre:ragcentre@127.0.0.1:54329/ragcentre",
        "dsn",
    ),
    Profile(
        "redis_flat",
        "redis",
        "redis",
        {"algorithm": "flat"},
        "RAGCENTRE_TEST_REDIS_URL",
        "redis://127.0.0.1:56379/0",
        "url",
    ),
    Profile(
        "redis_hnsw",
        "redis",
        "redis",
        {"algorithm": "hnsw"},
        "RAGCENTRE_TEST_REDIS_URL",
        "redis://127.0.0.1:56379/0",
        "url",
    ),
    Profile(
        "weaviate_server",
        "weaviate",
        "weaviate",
        {"grpc_port": 50051},
        "RAGCENTRE_TEST_WEAVIATE_URL",
        "http://127.0.0.1:58080",
        "url",
    ),
    Profile(
        "elasticsearch_hnsw",
        "elasticsearch",
        "elasticsearch",
        {"index_options": "hnsw"},
        "RAGCENTRE_TEST_ELASTICSEARCH_URL",
        "http://127.0.0.1:59200",
        "url",
    ),
    Profile(
        "elasticsearch_int8",
        "elasticsearch",
        "elasticsearch",
        {"index_options": "int8_hnsw"},
        "RAGCENTRE_TEST_ELASTICSEARCH_URL",
        "http://127.0.0.1:59200",
        "url",
    ),
    Profile(
        "opensearch_faiss",
        "opensearch",
        "opensearchpy",
        {"engine": "faiss"},
        "RAGCENTRE_TEST_OPENSEARCH_URL",
        "http://127.0.0.1:59201",
        "url",
    ),
    Profile(
        "opensearch_lucene",
        "opensearch",
        "opensearchpy",
        {"engine": "lucene"},
        "RAGCENTRE_TEST_OPENSEARCH_URL",
        "http://127.0.0.1:59201",
        "url",
    ),
    Profile("milvus_lite", "milvus", "pymilvus", {"uri": "workflow-milvus.db", "index_type": "FLAT"}),
    Profile(
        "milvus_server_flat",
        "milvus",
        "pymilvus",
        {"index_type": "FLAT"},
        "RAGCENTRE_TEST_MILVUS_URI",
        "http://127.0.0.1:59530",
        "uri",
    ),
    Profile(
        "milvus_server_hnsw",
        "milvus",
        "pymilvus",
        {"index_type": "HNSW"},
        "RAGCENTRE_TEST_MILVUS_URI",
        "http://127.0.0.1:59530",
        "uri",
    ),
    Profile(
        "pinecone_local",
        "pinecone",
        "pinecone",
        {},
        "RAGCENTRE_TEST_PINECONE_URL",
        "http://127.0.0.1:55080",
        "controller_host",
    ),
    Profile(
        "mongodb_exact",
        "mongodb",
        "pymongo",
        {"exact": True},
        "RAGCENTRE_TEST_MONGODB_URI",
        "mongodb://127.0.0.1:57017/?directConnection=true",
        "uri",
    ),
    Profile(
        "mongodb_approximate",
        "mongodb",
        "pymongo",
        {"exact": False},
        "RAGCENTRE_TEST_MONGODB_URI",
        "mongodb://127.0.0.1:57017/?directConnection=true",
        "uri",
    ),
    Profile("http_service", "http", "httpx", {}, "RAGCENTRE_TEST_HTTP_STORE_URL", None, "url"),
)


@contextmanager
def profile_endpoint(profile: Profile, tmp_path: Path) -> Iterator[str | None]:
    if profile.id == "http_service" and not os.environ.get("RAGCENTRE_TEST_HTTP_STORE_URL"):
        with sqlite_http_service(tmp_path / "http-store.sqlite") as endpoint:
            yield endpoint
    else:
        yield require_profile(profile)


@contextmanager
def sqlite_http_service(path: Path) -> Iterator[str]:
    import uvicorn
    from fastapi.responses import JSONResponse

    from ragcentre.errors import CollectionMismatch
    from ragcentre.stores.base import CollectionNotFound
    from ragcentre.stores.sqlite_vec import SqliteVecStore

    store = SqliteVecStore(path)
    app = FastAPI()
    fields: dict[str, dict[str, Any]] = {}

    @app.api_route("/{route:path}", methods=["GET", "PUT", "POST", "DELETE"])
    async def protocol(route: str, request: Request) -> Response:
        if route == "info":
            caps = asdict(store.capabilities)
            caps["metrics"] = sorted(caps["metrics"])
            return JSONResponse(
                {
                    "protocol": "ragcentre.vector_store/v1",
                    "service_name": "workflow-sqlite",
                    "service_version": "1.0.0",
                    "capabilities": caps,
                }
            )
        if route == "health":
            store.healthcheck()
            return JSONResponse({"ok": True})
        parts = route.split("/")
        if len(parts) < 2 or parts[0] != "collections":
            return JSONResponse({"error": {"code": "not_found", "message": "Unknown route"}}, status_code=404)
        name = parts[1]
        try:
            if request.method == "GET":
                info = store.collection_info(name)
                if info is None:
                    raise CollectionNotFound(name)
                return JSONResponse(asdict(info))
            if request.method == "DELETE":
                store.drop_collection(name)
                return Response(status_code=204)
            data = await request.json()
            if request.method == "PUT":
                store.ensure_collection(CollectionSpec(name, data["dim"], data["metric"], data["fields"]))
                fields[name] = data["fields"]
                return Response(status_code=204)
            if parts[2] == "upsert":
                store.upsert(name, [Record(**record) for record in data["records"]])
            elif parts[2] == "delete":
                store.delete(name, data["ids"])
            elif parts[2] == "query":
                flt = parse_filter(data.get("filter"), fields[name] | {"document_id": "str"})
                matches = store.query(name, data["vector"], data["k"], flt)
                return JSONResponse({"matches": [asdict(match) for match in matches]})
            return Response(status_code=204)
        except CollectionNotFound:
            return JSONResponse(
                {"error": {"code": "collection_not_found", "message": "Missing collection"}}, status_code=404
            )
        except CollectionMismatch:
            return JSONResponse(
                {"error": {"code": "collection_mismatch", "message": "Incompatible collection"}}, status_code=409
            )

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive(), "Persistent HTTP store exited before startup"
                assert time.monotonic() < deadline, "Persistent HTTP store failed to start"
                time.sleep(0.01)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "Persistent HTTP store failed to stop"
            store.close()


def require_profile(profile: Profile) -> str | None:
    if importlib.util.find_spec(profile.module) is None:
        pytest.skip(f"{profile.id}: client {profile.module} is not installed")
    if profile.id == "milvus_lite" and importlib.util.find_spec("milvus_lite") is None:
        pytest.skip("milvus_lite: milvus-lite is not installed for this platform")
    if profile.endpoint_env is None:
        return None
    endpoint = os.environ.get(profile.endpoint_env, profile.endpoint_default)
    if not endpoint:
        pytest.skip(f"{profile.id}: set {profile.endpoint_env} to a disposable local test service")
    parts = urlsplit(endpoint)
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip(f"{profile.id}: this suite is restricted to disposable loopback services")
    port = parts.port or {"http": 80, "https": 443, "postgresql": 5432, "redis": 6379, "mongodb": 27017}[parts.scheme]
    try:
        with socket.create_connection((parts.hostname, port), timeout=1):
            pass
    except OSError:
        pytest.skip(f"{profile.id}: no local service on port {port} ({profile.endpoint_env})")
    return endpoint


def body(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def eventually(check: Callable[[], Any], *, seconds: float = 30) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        try:
            return check()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def wait_ready(client: TestClient, pipeline_id: str) -> dict[str, Any]:
    def check() -> dict[str, Any]:
        state = body(client.get(f"/v1/pipelines/{pipeline_id}/status"))
        if state["recent_failures"]:
            pytest.fail(f"Pipeline ingestion failed: {state['recent_failures']}")
        assert state["status"] == "ready", state
        assert state["coverage"] == 1.0, state
        return state

    return eventually(check, seconds=60)


def pipeline_config(store: str, *, name: str = "vector-only") -> dict[str, Any]:
    return {
        "name": name,
        "nodes": [
            {"id": "parse", "type": "parse", "routes": [{"mime": "text/*", "provider": "builtin-text"}]},
            {
                "id": "chunk",
                "type": "chunk",
                "inputs": ["parse"],
                "strategy": "fixed",
                "options": {"max_chars": 1200, "overlap_chars": 0},
            },
            {"id": "embed", "type": "embed", "inputs": ["chunk"], "provider": "workflow-hash"},
            {
                "id": "vec",
                "type": "index_vector",
                "inputs": ["embed"],
                "provider": store,
                "options": {"metric": "cosine"},
            },
        ],
        "retrieval": {"sources": [{"id": "dense", "index": "vec", "top_k": 10}], "top_k": 10},
    }


@dataclass
class Workflow:
    profile: Profile
    ctx: AppContext
    client: TestClient
    provider: dict[str, Any]
    prefix: str
    sentinel: str

    def corpus(self, suffix: str = "documents") -> str:
        return body(
            self.client.post(
                "/v1/corpora",
                json={
                    "name": f"{self.prefix}{suffix}",
                    "metadata_schema": SCHEMA,
                },
            ),
            201,
        )["id"]

    def upload(
        self,
        corpus: str,
        key: str,
        *,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: int = 201,
    ) -> dict[str, Any]:
        return body(
            self.client.post(
                f"/v1/corpora/{corpus}/documents",
                files={
                    "file": ("shared-name.txt", (text or TEXTS[key]).encode(), "text/plain"),
                },
                data={"external_id": key, "metadata": json.dumps(metadata or METADATA[key])},
            ),
            status,
        )

    def build(self, corpus: str, *, store: str | None = None, name: str = "vector-only") -> str:
        config = pipeline_config(store or self.provider["name"], name=name)
        pipeline_id = body(self.client.post(f"/v1/corpora/{corpus}/pipelines", json=config), 201)["id"]
        wait_ready(self.client, pipeline_id)
        return pipeline_id

    def query(
        self,
        corpus: str,
        pipeline: str | None,
        *,
        query: str = TEXTS["apricot"],
        filter: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"query": query, **extra}
        if pipeline is not None:
            request["pipeline_id"] = pipeline
        if filter is not None:
            request["filter"] = filter
        return body(self.client.post(f"/v1/corpora/{corpus}/query", json=request))

    def index(self, pipeline: str) -> dict[str, Any]:
        row = self.ctx.db.one(
            "SELECT i.* FROM indexes i JOIN pipeline_nodes n ON i.node_fp = n.node_fp "
            "JOIN pipelines p ON p.id = n.pipeline_id AND p.corpus_id = i.corpus_id "
            "WHERE n.pipeline_id = ? AND i.kind = 'vector'",
            (pipeline,),
        )
        assert row is not None
        return dict(row)

    def stored_ids(self, pipeline: str, *, filter: dict[str, Any] | None = None) -> set[str]:
        index = self.index(pipeline)
        store = self.ctx.providers.store(index["provider_id"])
        info = store.collection_info(index["physical_name"])
        assert info is not None
        assert (info.dim, info.metric) == (DIM, "cosine")
        flt = parse_filter(filter, FIELD_TYPES) if filter is not None else None
        return {m.id for m in store.query(index["physical_name"], [1.0] + [0.0] * (DIM - 1), 10, flt)}

    def check_sentinel(self) -> None:
        store = self.ctx.providers.store(self.provider["id"])

        def check() -> None:
            info = store.collection_info(self.sentinel)
            assert info is not None and info.count == 1
            assert [m.id for m in store.query(self.sentinel, [1.0] + [0.0] * (DIM - 1), 1)] == [self.sentinel_id]

        eventually(check)

    @property
    def sentinel_id(self) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, self.sentinel))


def create_store(client: TestClient, adapter: str, name: str, config: dict[str, Any]) -> dict[str, Any]:
    return body(
        client.post(
            "/v1/providers",
            json={
                "name": name,
                "kind": "vector_store",
                "type": adapter,
                "config": config,
            },
        ),
        201,
    )


def opened_workflow(profile: Profile, endpoint: str | None, ctx: AppContext, client: TestClient) -> Iterator[Workflow]:
    prefix = f"uw_{uuid.uuid4().hex[:10]}_"
    config = dict(profile.config)
    secrets: dict[str, str] = {}
    pinecone_index: str | None = None
    provider: dict[str, Any] | None = None
    sentinel = "rc_sentinel_" + uuid.uuid4().hex[:12]
    if profile.adapter == "pgvector":
        config["schema"] = prefix.rstrip("_")
        secrets["dsn"] = str(endpoint)
    elif profile.adapter == "mongodb":
        config["database"] = prefix.rstrip("_")
        config["prefix"] = prefix
        secrets["uri"] = str(endpoint)
    else:
        if profile.adapter != "http":
            config["prefix"] = prefix
        if profile.endpoint_key and endpoint:
            if profile.id == "chroma_http":
                parts = urlsplit(endpoint)
                config.update(host=parts.hostname, port=parts.port or 80, ssl=parts.scheme == "https")
            else:
                config[profile.endpoint_key] = endpoint
    headers = {"Api-Key": "pinecone-local", "X-Pinecone-Api-Version": "2025-10"}
    try:
        if profile.adapter == "pinecone":
            pinecone_index = "uw-" + uuid.uuid4().hex[:12]
            body(
                httpx.post(
                    f"{endpoint}/indexes",
                    headers=headers,
                    json={
                        "name": pinecone_index,
                        "dimension": DIM,
                        "metric": "cosine",
                        "spec": {"serverless": {"cloud": "aws", "region": "us-east-1"}},
                    },
                    timeout=30,
                ),
                201,
            )
            config["index_name"] = pinecone_index
            secrets["api_key"] = "pinecone-local"
        provider = create_store(client, profile.adapter, "workflow-store", config)
        for key, value in secrets.items():
            body(client.put(f"/v1/providers/{provider['id']}/secrets/{key}", json={"value": value}))
        body(
            client.post(
                "/v1/providers",
                json={
                    "name": "workflow-hash",
                    "kind": "embedder",
                    "type": "builtin_hash",
                    "config": {"dimensions": DIM},
                },
            ),
            201,
        )
        health = body(client.post(f"/v1/providers/{provider['id']}/test"))
        assert health["ok"], health
        store = ctx.providers.store(provider["id"])
        store.ensure_collection(CollectionSpec(sentinel, DIM, "cosine"))
        instance = Workflow(profile, ctx, client, provider, prefix, sentinel)
        store.upsert(sentinel, [Record(instance.sentinel_id, [1.0] + [0.0] * (DIM - 1), {})])
        instance.check_sentinel()
        ctx.start_workers(2)
        yield instance
    finally:
        ctx.close_workers()
        ctx.providers.close()
        if provider is not None and profile.adapter not in {"pgvector", "mongodb"}:
            from ragcentre.stores.registry import vector_store_types

            spec = vector_store_types()[profile.adapter]
            cleanup_store = spec.factory(spec.config_model.model_validate(config), secrets, ctx.settings.data_dir)
            try:
                names = {sentinel} | {
                    row["physical_name"]
                    for row in ctx.db.all(
                        "SELECT physical_name FROM indexes WHERE provider_id = ? AND kind = 'vector'", (provider["id"],)
                    )
                }
                for name in names:
                    cleanup_store.drop_collection(name)
            finally:
                cleanup_store.close()
        if profile.adapter == "pgvector" and provider is not None:
            import psycopg
            from psycopg import sql

            with psycopg.connect(str(endpoint), autocommit=True) as connection:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(config["schema"])))
        if profile.adapter == "mongodb" and provider is not None:
            import pymongo

            with pymongo.MongoClient(str(endpoint), serverSelectionTimeoutMS=3000) as mongo:
                mongo.drop_database(config["database"])
        if pinecone_index is not None:
            reply = httpx.delete(f"{endpoint}/indexes/{pinecone_index}", headers=headers, timeout=30)
            assert reply.status_code in (200, 202, 204, 404), reply.text
