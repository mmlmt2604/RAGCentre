from __future__ import annotations

from collections.abc import Iterator

import pytest

pytest.importorskip("qdrant_client")

from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, Record, VectorStore
from ragcentre.stores.qdrant import QdrantConfig, QdrantStore
from ragcentre.stores.registry import vector_store_types
from ragcentre.testing.vector_store_contract import VectorStoreContract


class TestQdrantContract(VectorStoreContract):
    @pytest.fixture
    def store(self) -> Iterator[QdrantStore]:
        s = QdrantStore.connect(path=":memory:")
        yield s
        s.close()


def test_is_a_vector_store() -> None:
    s = QdrantStore.connect(path=":memory:")
    assert isinstance(s, VectorStore)
    assert s.capabilities.metrics == frozenset({"cosine", "l2", "dot"})
    assert s.capabilities.max_batch == 256
    s.close()


def test_missing_collection() -> None:
    s = QdrantStore.connect(path=":memory:")
    with pytest.raises(CollectionNotFound):
        s.query("rc_missing_0001", [1.0, 0.0], 3)
    with pytest.raises(CollectionNotFound):
        s.upsert("rc_missing_0001", [])
    s.delete("rc_missing_0001", ["6f9619ff-8b86-d011-b42d-00c04fc964ff"])
    s.close()


def test_drop_and_recreate_invalidates_cached_metric() -> None:
    s = QdrantStore.connect(path=":memory:")
    s.ensure_collection(CollectionSpec(name="rc_cache_0001", dim=2, metric="l2"))
    s.upsert("rc_cache_0001", [Record("6f9619ff-8b86-d011-b42d-00c04fc964ff", [3.0, 4.0], {})])
    assert s.query("rc_cache_0001", [0.0, 0.0], 1)[0].score == pytest.approx(-5.0)
    s.drop_collection("rc_cache_0001")
    s.ensure_collection(CollectionSpec(name="rc_cache_0001", dim=2, metric="dot"))
    s.upsert("rc_cache_0001", [Record("6f9619ff-8b86-d011-b42d-00c04fc964ff", [3.0, 4.0], {})])
    assert s.query("rc_cache_0001", [1.0, 1.0], 1)[0].score == pytest.approx(7.0)
    s.close()


def test_float_field_matches_integer_filter_values() -> None:
    s = QdrantStore.connect(path=":memory:")
    s.ensure_collection(CollectionSpec(name="rc_float_0001", dim=2, metric="cosine", fields={"score": "float"}))
    ids = ["6f9619ff-8b86-d011-b42d-00c04fc964f1", "6f9619ff-8b86-d011-b42d-00c04fc964f2"]
    s.upsert("rc_float_0001", [Record(ids[0], [1.0, 0.0], {"score": 1}), Record(ids[1], [0.0, 1.0], {"score": 0.5})])
    schema = {"score": "float", "document_id": "str"}
    for raw, want in [({"score": 1}, {ids[0]}), ({"score": {"$in": [1, 0.5]}}, set(ids)),
                      ({"score": {"$nin": [1]}}, {ids[1]}), ({"score": {"$ne": 0.5}}, {ids[0]})]:  # fmt: skip
        got = s.query("rc_float_0001", [1.0, 1.0], 5, parse_filter(raw, schema))  # type: ignore[arg-type]
        assert {m.id for m in got} == want, raw
    s.close()


def test_config_requires_one_location() -> None:
    with pytest.raises(ValueError):
        QdrantConfig.model_validate({})
    with pytest.raises(ValueError):
        QdrantConfig.model_validate({"url": "http://localhost:6333", "path": ":memory:"})


def test_registry_lists_qdrant(tmp_path: object) -> None:
    t = vector_store_types()["qdrant"]
    assert t.secret_keys == ("api_key",)
    store = t.factory(t.config_model.model_validate({"path": ":memory:"}), {}, tmp_path)  # type: ignore[arg-type]
    assert isinstance(store, QdrantStore)
    store.healthcheck()
    store.close()
