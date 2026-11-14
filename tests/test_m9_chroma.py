from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, Record, VectorStore
from ragcentre.stores.chroma import ChromaConfig, ChromaStore, translate_filter
from ragcentre.stores.registry import vector_store_types
from ragcentre.testing.vector_store_contract import VectorStoreContract


@pytest.fixture
def ephemeral() -> Iterator[ChromaStore]:
    s = ChromaStore.ephemeral()
    s._client.reset()  # ephemeral clients in one process share state
    yield s
    s._client.reset()
    s.close()


class TestChromaContract(VectorStoreContract):
    @pytest.fixture
    def store(self, ephemeral: ChromaStore) -> ChromaStore:
        return ephemeral


def test_is_a_vector_store(ephemeral: ChromaStore) -> None:
    assert isinstance(ephemeral, VectorStore)
    assert ephemeral.capabilities.metrics == frozenset({"cosine", "l2", "dot"})
    assert 1 <= ephemeral.client_max_batch <= ephemeral.capabilities.max_batch


def test_missing_collection(ephemeral: ChromaStore) -> None:
    with pytest.raises(CollectionNotFound):
        ephemeral.query("rc_missing_0001", [1.0, 0.0], 3)
    ephemeral.delete("rc_missing_0001", ["x"])
    ephemeral.drop_collection("rc_missing_0001")


def test_where_translation() -> None:
    schema = {"lang": "str", "score": "float", "public": "bool", "document_id": "str"}
    f = parse_filter({"lang": "en", "score": {"$in": [1, 0.5]}}, schema)  # type: ignore[arg-type]
    assert translate_filter(f, {"score": "float"}) == {
        "$and": [{"lang": {"$eq": "en"}}, {"score": {"$in": [1.0, 0.5]}}]
    }
    assert translate_filter(parse_filter({"public": True}, schema), {}) == {"public": {"$eq": True}}  # type: ignore[arg-type]


def test_metadata_free_records_and_squared_l2(ephemeral: ChromaStore) -> None:
    ephemeral.ensure_collection(CollectionSpec(name="rc_plain_0001", dim=2, metric="l2"))
    ephemeral.upsert("rc_plain_0001", [Record("a", [3.0, 4.0], {})])
    assert ephemeral.query("rc_plain_0001", [0.0, 0.0], 1)[0].score == pytest.approx(-5.0)


def test_upsert_splits_to_client_batch_size(ephemeral: ChromaStore) -> None:
    ephemeral.client_max_batch = 3
    ephemeral.ensure_collection(CollectionSpec(name="rc_split_0001", dim=2, metric="cosine"))
    ephemeral.upsert("rc_split_0001", [Record(f"r{i}", [1.0, float(i)], {"n": i}) for i in range(10)])
    info = ephemeral.collection_info("rc_split_0001")
    assert info is not None and info.count == 10


def test_config_and_persistent_registry(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ChromaConfig.model_validate({"host": "localhost", "path": "x"})
    t = vector_store_types()["chroma"]
    store = t.factory(t.config_model.model_validate({}), {}, tmp_path)
    assert isinstance(store, ChromaStore)
    store.healthcheck()
    store.ensure_collection(CollectionSpec(name="rc_disk_0001", dim=2, metric="dot"))
    assert (tmp_path / "chroma").is_dir()
