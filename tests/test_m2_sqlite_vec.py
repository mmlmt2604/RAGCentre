from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ragcentre.stores.base import CollectionSpec, StoreError, StoreUnavailable, VectorStore, batches
from ragcentre.stores.registry import vector_store_types
from ragcentre.stores.sqlite_vec import NO_EXTENSIONS_MESSAGE, SqliteVecStore, load_extension
from ragcentre.testing.vector_store_contract import VectorStoreContract


class TestSqliteVecContract(VectorStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[SqliteVecStore]:
        s = SqliteVecStore(tmp_path / "vectors.db")
        yield s
        s.close()


def test_is_a_vector_store(tmp_path: Path) -> None:
    s = SqliteVecStore(tmp_path / "v.db")
    assert isinstance(s, VectorStore)
    assert s.capabilities.metrics == frozenset({"cosine", "l2"})
    assert s.capabilities.max_batch == 1000
    s.close()


def test_missing_extension_support_gives_documented_error() -> None:
    class NoExtensions:  # like the macOS system Python's sqlite3.Connection
        pass

    with pytest.raises(StoreUnavailable) as exc:
        load_extension(NoExtensions())  # type: ignore[arg-type]
    assert str(exc.value) == NO_EXTENSIONS_MESSAGE
    assert "loadable SQLite extensions" in str(exc.value)


def test_rejects_unsupported_metric_and_bad_names(tmp_path: Path) -> None:
    s = SqliteVecStore(tmp_path / "v.db")
    with pytest.raises(StoreError):
        s.ensure_collection(CollectionSpec(name="rc_dot_00001", dim=4, metric="dot"))
    with pytest.raises(StoreError):
        s.ensure_collection(CollectionSpec(name='x"; DROP TABLE _collections; --', dim=4, metric="cosine"))
    with pytest.raises(StoreError):
        s.ensure_collection(CollectionSpec(name="rc_multi_0001", dim=4, metric="cosine", vector_kind="multi"))
    s.close()


def test_persists_across_reopen(tmp_path: Path) -> None:
    from ragcentre.stores.base import Record

    s = SqliteVecStore(tmp_path / "v.db")
    s.ensure_collection(CollectionSpec(name="rc_persist_01", dim=2, metric="l2"))
    s.upsert("rc_persist_01", [Record("a", [1.0, 0.0], {"document_id": "d"})])
    s.close()
    s2 = SqliteVecStore(tmp_path / "v.db")
    info = s2.collection_info("rc_persist_01")
    assert info is not None and info.count == 1 and info.metric == "l2"
    assert s2.query("rc_persist_01", [1.0, 0.0], 1)[0].score == pytest.approx(0.0)
    s2.close()


def test_zero_vector_does_not_sort_first(tmp_path: Path) -> None:
    from ragcentre.stores.base import Record

    s = SqliteVecStore(tmp_path / "v.db")
    s.ensure_collection(CollectionSpec(name="rc_zero_0001", dim=2, metric="cosine"))
    s.upsert("rc_zero_0001", [Record("z", [0.0, 0.0], {}), Record("a", [1.0, 0.0], {})])
    got = s.query("rc_zero_0001", [1.0, 0.1], 2)
    assert [m.id for m in got] == ["a", "z"]
    assert got[1].score == 0.0
    s.close()


def test_registry_lists_sqlite_vec(tmp_path: Path) -> None:
    types = vector_store_types()
    assert "sqlite_vec" in types
    t = types["sqlite_vec"]
    store = t.factory(t.config_model.model_validate({}), {}, tmp_path)
    assert isinstance(store, SqliteVecStore)
    assert store.path == tmp_path / "vectors.db"
    store.close()


def test_batches_helper() -> None:
    assert [len(b) for b in batches(list(range(7)), 3)] == [3, 3, 1]  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        list(batches([], 0))
