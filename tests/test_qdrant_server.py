from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any, ClassVar, Literal

import httpx
import pytest

from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import CollectionSpec, Metric, Record
from ragcentre.stores.qdrant import QdrantStore
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract

pytestmark = pytest.mark.integration
URL = os.environ.get("RAGCENTRE_TEST_QDRANT_URL", "http://127.0.0.1:56333")
GRPC_PORT = int(os.environ.get("RAGCENTRE_TEST_QDRANT_GRPC_PORT", "56334"))


@pytest.fixture(scope="module", autouse=True)
def server() -> None:
    pytest.importorskip("qdrant_client")
    try:
        response = httpx.get(f"{URL}/readyz", timeout=3)
        response.raise_for_status()
    except httpx.HTTPError:
        pytest.skip("Qdrant server unavailable; start tests/docker-compose.yml qdrant")


@pytest.fixture
def prefix() -> Iterator[str]:
    value = f"rcverify_{uuid.uuid4().hex}_"
    yield value
    with httpx.Client(base_url=URL, timeout=30) as client:
        for item in client.get("/collections").raise_for_status().json()["result"]["collections"]:
            if item["name"].startswith(value):
                client.delete(f"/collections/{item['name']}").raise_for_status()


@pytest.mark.parametrize("grpc", [False, True], ids=["http", "grpc"])
class TestQdrantServerContract(VectorStoreContract):
    @pytest.fixture
    def store(self, prefix: str, grpc: bool) -> Iterator[QdrantStore]:
        store = QdrantStore.connect(url=URL, prefix=prefix, prefer_grpc=grpc, grpc_port=GRPC_PORT, timeout=30)
        yield store
        store.close()


@pytest.mark.parametrize("grpc", [False, True], ids=["http", "grpc"])
def test_concurrent_creation_and_field_additions(prefix: str, grpc: bool) -> None:
    store = QdrantStore.connect(url=URL, prefix=prefix, prefer_grpc=grpc, grpc_port=GRPC_PORT, timeout=30)
    name = "rc_concurrent_0001"
    try:
        for create in (True, False):
            barrier = Barrier(4)

            def write(index: int, barrier: Barrier = barrier, create: bool = create) -> None:
                barrier.wait(timeout=10)
                fields: dict[str, Any] = {"group": "str"}
                metadata: dict[str, Any] = {"group": str(index)}
                if not create:
                    fields[f"field_{index}"] = "int"
                    metadata[f"field_{index}"] = index
                store.ensure_collection(CollectionSpec(name, 8, "cosine", fields))
                store.upsert(name, [Record(str(uuid.uuid4()), [1.0] + [0.0] * 7, metadata)])

            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(write, range(4)))
        info = store.collection_info(name)
        assert info is not None and info.count == 8
        native = httpx.get(f"{URL}/collections/{prefix}{name}", timeout=10).raise_for_status().json()["result"]
        expected = {"group": "str", **{f"field_{i}": "int" for i in range(4)}}
        assert native["config"]["metadata"]["ragcentre"]["fields"] == expected
        assert set(native["payload_schema"]) == {"document_id", *expected}
        for index in range(4):
            flt = parse_filter({f"field_{index}": index}, expected)
            assert len(store.query(name, [1.0] + [0.0] * 7, 8, flt)) == 1
    finally:
        store.close()


class TestQdrantServerOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"quantization": "none"}
    options_b: ClassVar[dict[str, Any]] = {"quantization": "scalar"}

    @pytest.fixture(autouse=True)
    def own_prefix(self, prefix: str) -> None:
        self.prefix = prefix

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> QdrantStore:
        return QdrantStore.connect(url=URL, prefix=self.prefix, timeout=30, **options)


@pytest.mark.parametrize("grpc", [False, True], ids=["http", "grpc"])
@pytest.mark.parametrize("quantization", ["none", "scalar", "binary"])
@pytest.mark.parametrize("metric", ["cosine", "dot", "l2"])
def test_effective_index_settings_and_filtered_retrieval(
    prefix: str, grpc: bool, quantization: Literal["none", "scalar", "binary"], metric: Metric
) -> None:
    store = QdrantStore.connect(
        url=URL,
        prefix=prefix,
        prefer_grpc=grpc,
        grpc_port=GRPC_PORT,
        timeout=30,
        hnsw_m=24,
        hnsw_ef_construction=128,
        hnsw_ef=96,
        quantization=quantization,
    )
    name = "rc_specialty_0001"
    dim = 128
    try:
        store.healthcheck()
        store.ensure_collection(CollectionSpec(name, dim, metric, {"group": "str", "active": "bool"}))
        wanted, other = str(uuid.uuid4()), str(uuid.uuid4())
        vector = [1.0] + [0.0] * (dim - 1)
        store.upsert(
            name,
            [
                Record(wanted, vector, {"group": "wanted", "active": True}),
                Record(other, [0.0, 1.0] + [0.0] * (dim - 2), {"group": "other", "active": False}),
            ],
        )
        flt = parse_filter({"$and": [{"group": "wanted"}, {"active": True}]}, {"group": "str", "active": "bool"})
        got = store.query(name, vector, 5, flt)
        assert [match.id for match in got] == [wanted]
        assert got[0].score == pytest.approx(0.0 if metric == "l2" else 1.0, abs=1e-4)
        native = httpx.get(f"{URL}/collections/{prefix}{name}", timeout=10).raise_for_status().json()["result"]
        assert native["config"]["hnsw_config"]["m"] == 24
        assert native["config"]["hnsw_config"]["ef_construct"] == 128
        actual = native["config"].get("quantization_config")
        assert actual is None if quantization == "none" else quantization in actual
        assert native["payload_schema"]["group"]["data_type"] == "keyword"
        assert native["payload_schema"]["active"]["data_type"] == "bool"
        store.delete(name, [wanted])
        assert store.query(name, vector, 5, flt) == []
    finally:
        store.close()
