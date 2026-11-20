"""Pinecone contracts against Pinecone Local, whose writes are immediately visible.

Cloud is eventually consistent; these tests intentionally make no cloud consistency claim.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

pytest.importorskip("pinecone")

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, Record, StoreError, VectorStore
from ragcentre.stores.pinecone import PineconeConfig, PineconeStore
from ragcentre.stores.registry import vector_store_types
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract

HOST = os.environ.get("RAGCENTRE_TEST_PINECONE_HOST", "http://localhost:5080")


def _open(tmp_path: Path, prefix: str, **options: Any) -> PineconeStore:
    return PineconeStore.connect(data_dir=tmp_path, api_key="pclocal", host=HOST, index_prefix=prefix, **options)


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PineconeStore]:
    s = _open(tmp_path_factory.mktemp("pinecone_check"), "check")
    try:
        s.healthcheck()
    except StoreError:
        s.close()
        if os.environ.get("RAGCENTRE_REQUIRE_VECTOR_SERVICES") == "1":
            pytest.fail(f"required Pinecone Local is unavailable at {HOST}")
        pytest.skip(f"no Pinecone Local at {HOST}")
    yield s
    s.close()


def _cleanup(s: PineconeStore, prefix: str) -> None:
    for index in s._client.list_indexes():
        if index.name.startswith(prefix + "-"):
            s._client.delete_index(index.name)


@pytest.mark.integration
class TestPineconeContract(VectorStoreContract):
    @pytest.fixture
    def store(self, server: PineconeStore, tmp_path: Path) -> Iterator[PineconeStore]:
        prefix = "t" + uuid.uuid4().hex[:8]
        s = _open(tmp_path, prefix)
        yield s
        _cleanup(s, prefix)
        s.close()


@pytest.mark.integration
class TestPineconeOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"region": "us-east-1"}
    options_b: ClassVar[dict[str, Any]] = {"region": "us-west-2"}

    @pytest.fixture(autouse=True)
    def prefix(self, server: PineconeStore) -> Iterator[None]:
        self._prefix = "o" + uuid.uuid4().hex[:8]
        yield
        _cleanup(server, self._prefix)

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> VectorStore:
        return _open(tmp_path, self._prefix, **options)


@pytest.mark.integration
def test_reopen_shape_and_metadata_replacement(server: PineconeStore, tmp_path: Path) -> None:
    prefix = "r" + uuid.uuid4().hex[:8]
    s = _open(tmp_path, prefix)
    name = "rc_reopen_001"
    record_id = str(uuid.uuid4())
    try:
        with pytest.raises(CollectionNotFound):
            s.query(name, [1.0, 0.0], 1)
        s.delete(name, [record_id])
        s.ensure_collection(CollectionSpec(name, 2, "l2", {"tag": "str", "old": "str"}))
        s.upsert(name, [Record(record_id, [3.0, 4.0], {"tag": "first", "old": "remove"})])
        assert s.query(name, [0.0, 0.0], 1)[0].score == pytest.approx(-5.0)
        s.close()
        s = _open(tmp_path, prefix)
        s.upsert(name, [Record(record_id, [0.0, 4.0], {"tag": "two words"})])
        fetched = s._index(name).fetch(ids=[record_id])
        assert "old" not in fetched.vectors[record_id].metadata
        f = parse_filter({"tag": "two words"}, {"tag": "str"})
        assert s.query(name, [0.0, 0.0], 1, f)[0].score == pytest.approx(-4.0)
        with pytest.raises(CollectionMismatch):
            s.ensure_collection(CollectionSpec(name, 2, "l2", {"tag": "int"}))
        with pytest.raises(StoreError):
            s.upsert(name, [Record(record_id, [1.0], {})])
        with pytest.raises(StoreError):
            s.query(name, [1.0], 1)
        assert s.query(name, [1.0, 0.0], 0) == []
        # If an index disappears outside the app, preserve CollectionNotFound even when a
        # persisted catalog entry still exists and the next client has no cached handle.
        s._client.delete_index(s._physical(name))
        s.close()
        s = _open(tmp_path, prefix)
        with pytest.raises(CollectionNotFound):
            s.query(name, [1.0, 0.0], 1)
    finally:
        _cleanup(s, prefix)
        s.close()


@pytest.mark.integration
def test_refuses_uncatalogued_remote_index(server: PineconeStore, tmp_path: Path) -> None:
    prefix = "c" + uuid.uuid4().hex[:8]
    s = _open(tmp_path, prefix)
    spec = CollectionSpec("rc_orphan_001", 2, "cosine")
    try:
        s.ensure_collection(spec)
        s._catalog.drop(spec.name)
        with pytest.raises(StoreError, match="no local catalog"):
            s.ensure_collection(spec)
        # An absent catalog must also prevent deleting a remote index of unknown ownership.
        s.drop_collection(spec.name)
        assert s._describe(spec.name) is not None
    finally:
        _cleanup(s, prefix)
        s.close()


def test_config_and_registration() -> None:
    t = vector_store_types()["pinecone"]
    assert t.secret_keys == ("api_key",)
    assert t.immutable_fields == ("cloud", "region")
    for config in ({"host": "ftp://host"}, {"host": "https://user:secret@host"}, {"index_prefix": "Bad_Prefix"}):
        with pytest.raises(ValueError):
            PineconeConfig.model_validate(config)


def test_physical_names_are_bounded_and_distinct(tmp_path: Path) -> None:
    s = _open(tmp_path, "longprefix")
    try:
        assert len(s._physical("a" * 63)) <= 45
        assert s._physical("rc_one_two") != s._physical("rc_onetwo")
    finally:
        s.close()
