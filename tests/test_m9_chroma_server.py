"""Chroma adapter against a real Chroma server (HTTP mode).

Needs a running server: ``docker run -p 58000:8000 chromadb/chroma`` (or ``chroma run --port 58000``).
The tests skip when none answers. Override the location with ``RAGCENTRE_TEST_CHROMA_HOST`` and
``RAGCENTRE_TEST_CHROMA_PORT``. Each test uses its own collection-name prefix and drops what it made.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

pytest.importorskip("chromadb")

from ragcentre.errors import CollectionMismatch
from ragcentre.stores.base import CollectionSpec, Record, StoreError
from ragcentre.stores.chroma import ChromaStore
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract

pytestmark = pytest.mark.integration

HOST = os.environ.get("RAGCENTRE_TEST_CHROMA_HOST", "localhost")
PORT = int(os.environ.get("RAGCENTRE_TEST_CHROMA_PORT", "58000"))


@pytest.fixture(scope="module")
def server() -> None:
    try:
        ChromaStore.http(HOST, PORT).healthcheck()
    except StoreError as exc:
        pytest.skip(f"no Chroma server at {HOST}:{PORT} ({str(exc).splitlines()[0][:120]})")


def _prefix() -> str:
    return f"t{uuid.uuid4().hex[:10]}_"


def _drop_prefixed(prefix: str, **kw: Any) -> None:
    store = ChromaStore.http(HOST, PORT, **kw)
    for col in store._client.list_collections(limit=1000):
        if col.name.startswith(prefix):
            store._client.delete_collection(col.name)


@pytest.fixture
def prefix(server: None) -> Iterator[str]:
    p = _prefix()
    yield p
    _drop_prefixed(p)


class TestChromaServerContract(VectorStoreContract):
    @pytest.fixture
    def store(self, prefix: str) -> ChromaStore:
        return ChromaStore.http(
            HOST, PORT, prefix=prefix, hnsw_max_neighbors=24, hnsw_ef_construction=150, ef_search=80
        )


class TestChromaServerOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"hnsw_max_neighbors": 16}
    options_b: ClassVar[dict[str, Any]] = {"hnsw_max_neighbors": 32}

    @pytest.fixture(autouse=True)
    def _server_prefix(self, prefix: str) -> None:
        self._pfx = prefix

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> ChromaStore:
        return ChromaStore.http(HOST, PORT, prefix=self._pfx, **options)


def test_max_batch_is_taken_from_the_server(prefix: str) -> None:
    s = ChromaStore.http(HOST, PORT, prefix=prefix)
    assert 1 <= s.client_max_batch <= s.capabilities.max_batch == 1000


def test_missing_tenant_is_a_clear_store_error(server: None) -> None:
    with pytest.raises(StoreError, match="tenant='no_such_tenant'"):
        ChromaStore.http(HOST, PORT, tenant="no_such_tenant", database="d")


def test_existing_tenant_and_database_are_used(prefix: str) -> None:
    import chromadb

    name = f"tn{uuid.uuid4().hex[:8]}"
    admin = chromadb.AdminClient(
        chromadb.config.Settings(
            anonymized_telemetry=False,
            chroma_api_impl="chromadb.api.fastapi.FastAPI",
            chroma_server_host=HOST,
            chroma_server_http_port=PORT,
        )
    )
    admin.create_tenant(name)
    admin.create_database("db1", name)
    try:
        scoped = ChromaStore.http(HOST, PORT, tenant=name, database="db1", prefix=prefix)
        scoped.ensure_collection(CollectionSpec("rc_ten_0001", 2, "cosine"))
        scoped.upsert("rc_ten_0001", [Record("a", [1.0, 0.0], {})])
        info = scoped.collection_info("rc_ten_0001")
        assert info is not None and info.count == 1
        assert ChromaStore.http(HOST, PORT, prefix=prefix).collection_info("rc_ten_0001") is None
        scoped.drop_collection("rc_ten_0001")
    finally:
        admin.delete_database("db1", name)


def test_changed_options_are_refused_by_the_server_collection(prefix: str) -> None:
    spec = CollectionSpec("rc_opt_0001", 2, "l2")
    ChromaStore.http(HOST, PORT, prefix=prefix, hnsw_ef_construction=100).ensure_collection(spec)
    with pytest.raises(CollectionMismatch):
        ChromaStore.http(HOST, PORT, prefix=prefix, hnsw_ef_construction=200).ensure_collection(spec)
