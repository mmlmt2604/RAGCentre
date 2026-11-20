"""Milvus adapter: the contract suite on Milvus Lite, and on a Milvus server when one is running
(``docker compose -f tests/docker-compose.yml up -d milvus``, then ``pytest -m integration``)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

pytest.importorskip("pymilvus")

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, Record, StoreError, VectorStore
from ragcentre.stores.milvus import MilvusConfig, MilvusStore, translate_filter
from ragcentre.stores.registry import vector_store_types
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract

MILVUS_URI = os.environ.get("RAGCENTRE_TEST_MILVUS_URI", "http://127.0.0.1:19530")


def _has_lite() -> bool:
    try:
        import milvus_lite  # noqa: F401
    except ImportError:
        return False
    return True


lite = pytest.mark.skipif(not _has_lite(), reason="needs the milvus-lite extra")


@lite
class TestMilvusLiteContract(VectorStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[MilvusStore]:
        s = MilvusStore.connect(uri=str(tmp_path / "milvus.db"), data_dir=tmp_path, index_type="FLAT")
        yield s
        s.close()


def _server_available() -> bool:
    try:
        from pymilvus import MilvusClient

        MilvusClient(uri=MILVUS_URI, timeout=2).close()
    except Exception:
        return False
    return True


@pytest.fixture(scope="module")
def milvus_server() -> str:
    if not _server_available():
        if os.environ.get("RAGCENTRE_REQUIRE_VECTOR_SERVICES") == "1":
            pytest.fail(f"required Milvus server is unavailable at {MILVUS_URI}")
        pytest.skip(f"no Milvus server at {MILVUS_URI}")
    return MILVUS_URI


def _server_store(uri: str, data_dir: Path, **kw: Any) -> MilvusStore:
    # A unique prefix isolates each test's collections on the shared server.
    return MilvusStore.connect(uri=uri, data_dir=data_dir, **kw)


@pytest.mark.integration
class TestMilvusServerContract(VectorStoreContract):
    @pytest.fixture
    def store(self, milvus_server: str, tmp_path: Path) -> Iterator[MilvusStore]:
        prefix = f"t{uuid.uuid4().hex[:8]}_"
        s = _server_store(milvus_server, tmp_path, index_type="FLAT", collection_prefix=prefix)
        yield s
        for name in s._client.list_collections():
            if name.startswith(prefix):
                s._client.drop_collection(name)
        s.close()


@pytest.mark.integration
class TestMilvusServerHnsw(VectorStoreContract):
    """HNSW is approximate, so filtered queries are held to recall rather than the exact top-k."""

    @pytest.fixture
    def store(self, milvus_server: str, tmp_path: Path) -> Iterator[MilvusStore]:
        prefix = f"h{uuid.uuid4().hex[:8]}_"
        s = _server_store(milvus_server, tmp_path, index_type="HNSW", collection_prefix=prefix)
        yield s
        for name in s._client.list_collections():
            if name.startswith(prefix):
                s._client.drop_collection(name)
        s.close()


@pytest.mark.integration
class TestMilvusServerOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"index_type": "FLAT"}
    options_b: ClassVar[dict[str, Any]] = {"index_type": "HNSW"}

    @pytest.fixture(autouse=True)
    def _prefix(self, milvus_server: str) -> Iterator[None]:
        self.prefix = f"o{uuid.uuid4().hex[:8]}_"
        yield
        from pymilvus import MilvusClient

        client = MilvusClient(uri=milvus_server)
        for name in client.list_collections():
            if name.startswith(self.prefix):
                client.drop_collection(name)
        client.close()

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> VectorStore:
        return _server_store(MILVUS_URI, tmp_path, collection_prefix=self.prefix, **options)


# ---- unit tests (no server) -----------------------------------------------------------------


def test_filter_translation() -> None:
    schema = {"lang": "str", "year": "int", "score": "float", "ok": "bool", "document_id": "str"}
    raw = {"$or": [{"lang": {"$in": ["en", 'a"b']}}, {"$and": [{"year": {"$gte": 2020}}, {"ok": False}]},
                   {"score": {"$nin": [1, 0.5]}}]}  # fmt: skip
    f = parse_filter(raw, schema)  # type: ignore[arg-type]
    assert f is not None
    assert translate_filter(f) == (
        '((meta["lang"] == "en" or meta["lang"] == "a\\"b") or '
        '((meta["year"] >= 2020) and (meta["ok"] == false)) or '
        '(meta["score"] != 1 and meta["score"] != 0.5))'
    )


def test_config_validates_prefix() -> None:
    with pytest.raises(ValueError):
        MilvusConfig.model_validate({"collection_prefix": "Bad-Prefix"})
    assert MilvusConfig.model_validate({}).uri == "milvus.db"
    with pytest.raises(ValueError):
        MilvusConfig.model_validate({"uri": "some/dir"})


def test_registry_lists_milvus() -> None:
    t = vector_store_types()["milvus"]
    assert t.secret_keys == ("token",)
    assert t.immutable_fields == ("index_type",)
    assert t.capabilities.metrics == frozenset({"cosine", "l2", "dot"})


@lite
def test_factory_puts_lite_under_data_dir(tmp_path: Path) -> None:
    t = vector_store_types()["milvus"]
    store = t.factory(t.config_model.model_validate({"uri": "lite.db"}), {}, tmp_path)
    assert isinstance(store, MilvusStore)
    store.healthcheck()
    store.close()
    assert (tmp_path / "lite.db").exists()
    assert (tmp_path / "store_catalogs" / "milvus.db").exists()


@lite
def test_missing_collection_and_reopen(tmp_path: Path) -> None:
    s = MilvusStore.connect(uri=str(tmp_path / "m.db"), data_dir=tmp_path, index_type="FLAT")
    with pytest.raises(CollectionNotFound):
        s.query("rc_missing_0001", [1.0, 0.0], 3)
    s.delete("rc_missing_0001", [str(uuid.uuid4())])
    s.ensure_collection(CollectionSpec(name="rc_reopen_0001", dim=2, metric="l2", fields={"n": "int"}))
    s.upsert("rc_reopen_0001", [Record(str(uuid.uuid4()), [3.0, 4.0], {"n": 1})])
    s.close()
    # A new client sees the collection "released"; the adapter loads it before searching.
    s = MilvusStore.connect(uri=str(tmp_path / "m.db"), data_dir=tmp_path, index_type="FLAT")
    assert s.query("rc_reopen_0001", [0.0, 0.0], 1)[0].score == pytest.approx(-5.0)
    with pytest.raises(CollectionMismatch):
        s.ensure_collection(CollectionSpec(name="rc_reopen_0001", dim=2, metric="l2", fields={"n": "str"}))
    s.close()


@lite
def test_refuses_uncatalogued_remote_collection(tmp_path: Path) -> None:
    s = MilvusStore.connect(uri=str(tmp_path / "m.db"), data_dir=tmp_path, index_type="FLAT")
    spec = CollectionSpec("rc_orphan_001", 2, "cosine")
    try:
        s.ensure_collection(spec)
        s._catalog.drop(spec.name)
        with pytest.raises(StoreError, match="no local catalog"):
            s.ensure_collection(spec)
    finally:
        s.drop_collection(spec.name)
        s.close()


@lite
def test_index_type_is_recorded(tmp_path: Path) -> None:
    s = MilvusStore.connect(uri=str(tmp_path / "m.db"), data_dir=tmp_path, index_type="FLAT")
    s.ensure_collection(CollectionSpec(name="rc_opts_0001", dim=2, metric="cosine"))
    s.close()
    s = MilvusStore.connect(uri=str(tmp_path / "m.db"), data_dir=tmp_path, index_type="AUTOINDEX")
    with pytest.raises(CollectionMismatch):
        s.ensure_collection(CollectionSpec(name="rc_opts_0001", dim=2, metric="cosine"))
    s.close()
