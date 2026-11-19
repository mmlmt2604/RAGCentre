from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

pytest.importorskip("qdrant_client")

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, Record, StoreError, VectorStore
from ragcentre.stores.qdrant import QdrantConfig, QdrantStore
from ragcentre.stores.registry import vector_store_types
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract


class TestQdrantContract(VectorStoreContract):
    @pytest.fixture
    def store(self) -> Iterator[QdrantStore]:
        s = QdrantStore.connect(path=":memory:")
        yield s
        s.close()


class TestQdrantOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"quantization": "none"}
    options_b: ClassVar[dict[str, Any]] = {"quantization": "scalar"}

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> QdrantStore:
        return QdrantStore.connect(path=str(tmp_path / "qdrant"), **options)


def test_is_a_vector_store() -> None:
    s = QdrantStore.connect(path=":memory:")
    assert isinstance(s, VectorStore)
    assert s.capabilities.metrics == frozenset({"cosine", "l2", "dot"})
    assert s.capabilities.max_batch == 256
    assert s.capabilities.max_dim is None  # the client exposes no dimension limit
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


def test_config_accepts_and_validates_new_fields() -> None:
    cfg = QdrantConfig.model_validate(
        {
            "path": ":memory:",
            "prefer_grpc": False,
            "grpc_port": 6334,
            "timeout": 10,
            "prefix": "tenant_",
            "hnsw_m": 16,
            "hnsw_ef_construction": 100,
            "quantization": "scalar",
            "hnsw_ef": 128,
        }
    )
    assert (cfg.hnsw_m, cfg.hnsw_ef_construction, cfg.quantization, cfg.hnsw_ef) == (16, 100, "scalar", 128)
    assert cfg.prefix == "tenant_" and cfg.grpc_port == 6334 and cfg.timeout == 10
    with pytest.raises(ValueError):
        QdrantConfig.model_validate({"path": ":memory:", "prefix": "bad/prefix"})
    with pytest.raises(ValueError):
        QdrantConfig.model_validate({"path": ":memory:", "quantization": "product"})
    with pytest.raises(ValueError):
        QdrantConfig.model_validate({"path": ":memory:", "hnsw_m": 1})


def test_prefix_maps_physical_name(tmp_path: Path) -> None:
    db = tmp_path / "qdrant"
    prefixed = QdrantStore.connect(path=str(db), prefix="tenant_")
    try:
        prefixed.ensure_collection(CollectionSpec(name="rc_pref_0001", dim=2, metric="cosine"))
        assert prefixed._physical("rc_pref_0001") == "tenant_rc_pref_0001"
        prefixed.upsert("rc_pref_0001", [Record("6f9619ff-8b86-d011-b42d-00c04fc964ff", [1.0, 0.0], {})])
        assert prefixed.collection_info("rc_pref_0001") is not None
    finally:
        prefixed.close()
    plain = QdrantStore.connect(path=str(db))
    try:
        assert plain.collection_info("rc_pref_0001") is None  # stored under the prefixed name only
    finally:
        plain.close()


def test_prefix_that_breaks_naming_is_rejected() -> None:
    s = QdrantStore.connect(path=":memory:", prefix="-bad")  # leading '-' is not a valid Qdrant name start
    try:
        with pytest.raises(StoreError, match="valid Qdrant name"):
            s.ensure_collection(CollectionSpec(name="rc_pref_0001", dim=2, metric="cosine"))
    finally:
        s.close()


def test_hnsw_options_recorded_and_checked(tmp_path: Path) -> None:
    db = tmp_path / "qdrant_hnsw"
    a = QdrantStore.connect(path=str(db), hnsw_m=16, hnsw_ef_construction=200)
    try:
        a.ensure_collection(CollectionSpec(name="rc_hnsw_0001", dim=2, metric="cosine"))
    finally:
        a.close()
    b = QdrantStore.connect(path=str(db), hnsw_m=32, hnsw_ef_construction=200)
    try:
        with pytest.raises(CollectionMismatch, match="hnsw_m"):
            b.ensure_collection(CollectionSpec(name="rc_hnsw_0001", dim=2, metric="cosine"))
    finally:
        b.close()
    again = QdrantStore.connect(path=str(db), hnsw_m=16, hnsw_ef_construction=200)
    try:
        again.ensure_collection(CollectionSpec(name="rc_hnsw_0001", dim=2, metric="cosine"))
        assert again.collection_info("rc_hnsw_0001") is not None
    finally:
        again.close()


def test_collection_built_without_options_still_opens(tmp_path: Path) -> None:
    from qdrant_client import QdrantClient, models

    db = tmp_path / "qdrant_old"
    client = QdrantClient(path=str(db))
    client.create_collection(
        "rc_old_0001",
        vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
        metadata={"ragcentre": {"fields": {"lang": "str"}}},  # pre-V4 layout: no "options"
    )
    client.close()
    s = QdrantStore.connect(path=str(db))
    try:
        s.ensure_collection(CollectionSpec(name="rc_old_0001", dim=2, metric="cosine", fields={"lang": "str"}))
        s.upsert("rc_old_0001", [Record("6f9619ff-8b86-d011-b42d-00c04fc964ff", [1.0, 0.0], {"lang": "en"})])
        assert s.query("rc_old_0001", [1.0, 0.0], 1)[0].id == "6f9619ff-8b86-d011-b42d-00c04fc964ff"
    finally:
        s.close()


def test_hnsw_ef_query_time_option() -> None:
    s = QdrantStore.connect(path=":memory:", hnsw_ef=64)
    try:
        s.ensure_collection(CollectionSpec(name="rc_ef_0001", dim=2, metric="cosine"))
        s.upsert("rc_ef_0001", [Record("6f9619ff-8b86-d011-b42d-00c04fc964ff", [1.0, 0.0], {})])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # local mode ignores search_params
            assert s.query("rc_ef_0001", [1.0, 0.0], 1)[0].score == pytest.approx(1.0)
    finally:
        s.close()


def test_healthcheck_rejects_old_server_version() -> None:
    class FakeInfo:
        version = "1.15.0"

    class FakeClient:
        def info(self) -> FakeInfo:
            return FakeInfo()

        def close(self) -> None:
            pass

    s = QdrantStore(FakeClient(), local=False)  # type: ignore[arg-type]
    with pytest.raises(StoreError, match=r"1\.16"):
        s.healthcheck()
    s.close()


def test_registry_lists_qdrant(tmp_path: object) -> None:
    t = vector_store_types()["qdrant"]
    assert t.secret_keys == ("api_key",)
    assert t.immutable_fields == ("hnsw_m", "hnsw_ef_construction", "quantization")
    store = t.factory(t.config_model.model_validate({"path": ":memory:"}), {}, tmp_path)  # type: ignore[arg-type]
    assert isinstance(store, QdrantStore)
    store.healthcheck()
    store.close()
