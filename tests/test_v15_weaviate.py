"""Weaviate contracts against the HTTP + gRPC service in tests/docker-compose.yml."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

pytest.importorskip("weaviate")

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, Record, StoreError, VectorStore
from ragcentre.stores.registry import vector_store_types
from ragcentre.stores.weaviate import WeaviateConfig, WeaviateStore
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract

URL = os.environ.get("RAGCENTRE_TEST_WEAVIATE_URL", "http://localhost:58080")
GRPC_PORT = int(os.environ.get("RAGCENTRE_TEST_WEAVIATE_GRPC_PORT", "50051"))


@pytest.fixture(scope="module")
def server() -> Iterator[WeaviateStore]:
    try:
        s = WeaviateStore.connect(url=URL, grpc_port=GRPC_PORT)
        s.healthcheck()
    except StoreError:
        if os.environ.get("RAGCENTRE_REQUIRE_VECTOR_SERVICES") == "1":
            pytest.fail(f"required Weaviate service is unavailable at {URL}")
        pytest.skip(f"no Weaviate service at {URL}")
    yield s
    s.close()


def _open(prefix: str, **options: Any) -> WeaviateStore:
    return WeaviateStore.connect(url=URL, grpc_port=GRPC_PORT, collection_prefix=prefix, **options)


def _cleanup(s: WeaviateStore, prefix: str) -> None:
    for name in s._client.collections.list_all(simple=True):
        if name.startswith(prefix + "_"):
            s._client.collections.delete(name)


@pytest.mark.integration
class TestWeaviateContract(VectorStoreContract):
    @pytest.fixture
    def store(self, server: WeaviateStore) -> Iterator[WeaviateStore]:
        prefix = "T" + uuid.uuid4().hex[:10]
        s = _open(prefix)
        yield s
        _cleanup(s, prefix)
        s.close()


@pytest.mark.integration
class TestWeaviateHnsw(TestWeaviateContract):
    @pytest.fixture
    def store(self, server: WeaviateStore) -> Iterator[WeaviateStore]:
        prefix = "H" + uuid.uuid4().hex[:10]
        s = _open(prefix, index_type="hnsw")
        yield s
        _cleanup(s, prefix)
        s.close()


@pytest.mark.integration
class TestWeaviateOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"index_type": "flat"}
    options_b: ClassVar[dict[str, Any]] = {"index_type": "hnsw"}

    @pytest.fixture(autouse=True)
    def prefix(self, server: WeaviateStore) -> Iterator[None]:
        self._prefix = "O" + uuid.uuid4().hex[:10]
        yield
        _cleanup(server, self._prefix)

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> VectorStore:
        return _open(self._prefix, **options)


@pytest.mark.integration
def test_reopen_and_complete_metadata_replacement(server: WeaviateStore) -> None:
    prefix = "R" + uuid.uuid4().hex[:10]
    s = _open(prefix)
    name = "rc_reopen_001"
    record_id = str(uuid.uuid4())
    try:
        with pytest.raises(CollectionNotFound):
            s.query(name, [1.0, 0.0], 1)
        s.ensure_collection(CollectionSpec(name, 2, "dot", {"tag": "str", "old": "str"}))
        s.upsert(name, [Record(record_id, [3.0, 4.0], {"tag": "two words", "old": "remove"})])
        s.close()
        s = _open(prefix)
        assert s.collection_info(name).dim == 2
        s.upsert(name, [Record(record_id, [4.0, 3.0], {"tag": "two words"})])
        obj = s._collection(name).query.fetch_object_by_id(record_id)
        assert obj.properties.get("m_old") is None
        f = parse_filter({"tag": "two words"}, {"tag": "str"})
        assert s.query(name, [1.0, 0.0], 1, f)[0].score == pytest.approx(4.0)
        with pytest.raises(CollectionMismatch):
            s.ensure_collection(CollectionSpec(name, 2, "dot", {"tag": "int"}))
        with pytest.raises(StoreError):
            s.upsert(name, [Record(record_id, [1.0], {})])
        with pytest.raises(StoreError):
            s.query(name, [1.0], 1)
        assert s.query(name, [1.0, 0.0], 0) == []
    finally:
        _cleanup(s, prefix)
        s.close()


def test_config_and_registration() -> None:
    t = vector_store_types()["weaviate"]
    assert t.secret_keys == ("api_key",)
    assert t.immutable_fields == ("index_type",)
    for config in ({"url": "ftp://host"}, {"url": "https://user:secret@host"}, {"collection_prefix": "lowercase"}):
        with pytest.raises(ValueError):
            WeaviateConfig.model_validate(config)
