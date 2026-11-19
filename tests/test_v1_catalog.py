from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ragcentre.errors import CollectionMismatch
from ragcentre.stores.base import CollectionSpec, StoreError
from ragcentre.stores.catalog import Catalog, CatalogEntry, InStoreCatalog, LocalCatalog, reconcile
from vs_support import MemoryStore

SPEC = CollectionSpec("rc_cat_0001", 8, "cosine", {"lang": "str"})


# ---- entries and the shared rules ------------------------------------------------------------


def test_entry_json_round_trip() -> None:
    entry = CatalogEntry(8, "l2", {"lang": "str", "year": "int"}, {"quantization": "scalar", "m": 16})
    assert CatalogEntry.from_json(entry.to_json()) == entry


@pytest.mark.parametrize("text", ["not json", "{}", '{"dim": "x", "metric": "cosine"}', "[]"])
def test_unreadable_entry_is_a_store_error(text: str) -> None:
    with pytest.raises(StoreError, match="unreadable collection catalog entry"):
        CatalogEntry.from_json(text)


def test_reconcile_new_same_and_merged_fields() -> None:
    created = reconcile(None, SPEC, {"a": 1})
    assert created == CatalogEntry(8, "cosine", {"lang": "str"}, {"a": 1})
    assert reconcile(created, SPEC, {"a": 1}) is created  # nothing to write
    wider = CollectionSpec(SPEC.name, 8, "cosine", {"lang": "str", "year": "int"})
    merged = reconcile(created, wider, {"a": 1})
    assert merged.fields == {"lang": "str", "year": "int"}
    # Fields are only ever added: a narrower spec keeps what was recorded.
    assert reconcile(merged, SPEC, {"a": 1}) is merged


def test_reconcile_mismatches() -> None:
    entry = CatalogEntry(8, "cosine", {"lang": "str"}, {"q": "none"})
    with pytest.raises(CollectionMismatch, match="dim=8"):
        reconcile(entry, CollectionSpec(SPEC.name, 9, "cosine", {}), {"q": "none"})
    with pytest.raises(CollectionMismatch, match="metric=cosine"):
        reconcile(entry, CollectionSpec(SPEC.name, 8, "l2", {}), {"q": "none"})
    with pytest.raises(CollectionMismatch, match="field lang is str"):
        reconcile(entry, CollectionSpec(SPEC.name, 8, "cosine", {"lang": "int"}), {"q": "none"})
    with pytest.raises(CollectionMismatch, match="create a new store provider"):
        reconcile(entry, SPEC, {"q": "scalar"})
    with pytest.raises(CollectionMismatch):
        reconcile(entry, SPEC, None)  # options were recorded; none requested now


def test_options_compare_after_json_normalisation() -> None:
    entry = reconcile(None, SPEC, {"sizes": (1, 2), "b": 1, "a": 2})
    assert reconcile(entry, SPEC, {"a": 2, "sizes": [1, 2], "b": 1}) is entry


# ---- both catalogs satisfy the same behaviour ------------------------------------------------


def _in_store(tmp_path: Path) -> InStoreCatalog:
    texts: dict[str, str] = {}

    def remove(name: str) -> None:
        texts.pop(name, None)

    return InStoreCatalog(texts.get, texts.__setitem__, remove)


def _local(tmp_path: Path) -> LocalCatalog:
    return LocalCatalog(tmp_path / "data", "fake_store", "instance-1")


@pytest.fixture(params=["in_store", "local"])
def catalog(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Catalog]:
    made: Catalog = (_in_store if request.param == "in_store" else _local)(tmp_path)
    yield made
    if isinstance(made, LocalCatalog):
        made.close()


def test_both_are_catalogs(catalog: Catalog) -> None:
    assert isinstance(catalog, Catalog)


def test_get_ensure_drop(catalog: Catalog) -> None:
    assert catalog.get(SPEC.name) is None
    entry = catalog.ensure(SPEC, {"hnsw_m": 16})
    assert catalog.get(SPEC.name) == entry == CatalogEntry(8, "cosine", {"lang": "str"}, {"hnsw_m": 16})
    assert catalog.ensure(SPEC, {"hnsw_m": 16}) == entry
    with pytest.raises(CollectionMismatch):
        catalog.ensure(CollectionSpec(SPEC.name, 4, "cosine"), {"hnsw_m": 16})
    with pytest.raises(CollectionMismatch):
        catalog.ensure(SPEC, {"hnsw_m": 32})
    assert catalog.get(SPEC.name) == entry  # a refused ensure changes nothing
    grown = catalog.ensure(CollectionSpec(SPEC.name, 8, "cosine", {"year": "int"}), {"hnsw_m": 16})
    assert grown.fields == {"lang": "str", "year": "int"} and catalog.get(SPEC.name) == grown
    catalog.drop(SPEC.name)
    catalog.drop(SPEC.name)  # idempotent
    catalog.drop("rc_never_0001")
    assert catalog.get(SPEC.name) is None
    # After a drop the name can be built again with a different shape.
    assert catalog.ensure(CollectionSpec(SPEC.name, 3, "l2"), None).dim == 3


def test_concurrent_ensure_same_spec_writes_once(catalog: Catalog) -> None:
    results: list[CatalogEntry] = []
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def work() -> None:
        try:
            start.wait()
            results.append(catalog.ensure(SPEC, {"q": "none"}))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert len(results) == 8 and len(set(results[i].to_json() for i in range(8))) == 1
    assert catalog.get(SPEC.name) == results[0]


def test_concurrent_ensure_conflicting_dims_has_one_winner(catalog: Catalog) -> None:
    outcomes: list[str] = []
    start = threading.Barrier(6)

    def work(dim: int) -> None:
        start.wait()
        try:
            catalog.ensure(CollectionSpec(SPEC.name, dim, "cosine"), None)
            outcomes.append(f"ok{dim}")
        except CollectionMismatch:
            outcomes.append("mismatch")

    threads = [threading.Thread(target=work, args=(2 + i % 3,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(outcomes) == 6
    winner = catalog.get(SPEC.name)
    assert winner is not None
    # Every caller whose dimension is the winner's succeeded; every other one was refused.
    assert sorted(outcomes) == sorted(f"ok{winner.dim}" if (2 + i % 3) == winner.dim else "mismatch" for i in range(6))


# ---- InStoreCatalog specifics ----------------------------------------------------------------


def test_in_store_remove_is_optional() -> None:
    texts: dict[str, str] = {}
    cat = InStoreCatalog(texts.get, texts.__setitem__)
    cat.ensure(SPEC)
    cat.drop(SPEC.name)  # no remove callable: nothing happens, and that is allowed
    assert cat.get(SPEC.name) is not None


def test_in_store_detects_a_competing_writer() -> None:
    """Another process creates the collection between our read and our write."""
    texts: dict[str, str] = {}
    rival = CatalogEntry(16, "cosine", {}, {})

    def write(name: str, text: str) -> None:
        texts[name] = rival.to_json()  # the rival's write lands last

    cat = InStoreCatalog(texts.get, write)
    with pytest.raises(CollectionMismatch, match="dim=16"):
        cat.ensure(SPEC)
    # If the rival wrote exactly the same thing there is no error.
    same = CatalogEntry(8, "cosine", {"lang": "str"}, {})
    texts.clear()
    cat2 = InStoreCatalog(texts.get, lambda n, t: texts.__setitem__(n, same.to_json()))
    assert cat2.ensure(SPEC) == same


def test_in_store_serialises_threads_of_one_process() -> None:
    texts: dict[str, str] = {}
    writes: list[str] = []

    def slow_read(name: str) -> str | None:
        time.sleep(0.01)
        return texts.get(name)

    def write(name: str, text: str) -> None:
        writes.append(name)
        texts[name] = text

    cat = InStoreCatalog(slow_read, write)
    threads = [threading.Thread(target=cat.ensure, args=(SPEC,)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert writes == [SPEC.name]


def test_in_store_shared_transaction_preserves_concurrent_field_additions() -> None:
    texts: dict[str, str] = {}
    lock = threading.RLock()
    start = threading.Barrier(6)

    def read(name: str) -> str | None:
        text = texts.get(name)
        time.sleep(0.002)  # widen the read/modify/write race
        return text

    def work(i: int) -> None:
        catalog = InStoreCatalog(read, texts.__setitem__, transaction=lambda: lock)
        start.wait(timeout=10)
        catalog.ensure(CollectionSpec(SPEC.name, 8, "cosine", {f"field_{i}": "int"}))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(work, range(6)))
    entry = CatalogEntry.from_json(texts[SPEC.name])
    assert entry.fields == {f"field_{i}": "int" for i in range(6)}


def test_in_store_failed_write_is_not_reported_as_success() -> None:
    cat = InStoreCatalog(lambda name: None, lambda name, text: None)
    with pytest.raises(StoreError, match="not visible"):
        cat.ensure(SPEC)


# ---- LocalCatalog specifics ------------------------------------------------------------------


def test_local_catalog_file_location_and_persistence(tmp_path: Path) -> None:
    cat = LocalCatalog(tmp_path, "pinecone", "key-a")
    assert cat.path == tmp_path / "store_catalogs" / "pinecone.db" and cat.path.exists()
    cat.ensure(SPEC, {"x": 1})
    cat.close()
    again = LocalCatalog(tmp_path, "pinecone", "key-a")
    assert again.get(SPEC.name) == CatalogEntry(8, "cosine", {"lang": "str"}, {"x": 1})
    again.close()


def test_local_catalog_instances_do_not_see_each_other(tmp_path: Path) -> None:
    a = LocalCatalog(tmp_path, "pinecone", "host-a/index-1")
    b = LocalCatalog(tmp_path, "pinecone", "host-a/index-2")
    a.ensure(SPEC)
    assert b.get(SPEC.name) is None
    b.ensure(CollectionSpec(SPEC.name, 3, "l2"))  # same collection name, other instance: no conflict
    assert a.get(SPEC.name) is not None and a.get(SPEC.name) != b.get(SPEC.name)
    b.drop(SPEC.name)
    assert a.get(SPEC.name) is not None
    other_type = LocalCatalog(tmp_path, "weaviate", "host-a/index-1")
    assert other_type.get(SPEC.name) is None and other_type.path != a.path


def test_local_catalog_lost_file_means_unknown_collections(tmp_path: Path) -> None:
    cat = LocalCatalog(tmp_path, "pinecone", "k")
    cat.ensure(SPEC)
    cat.close()
    cat.path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(cat.path) + suffix).unlink(missing_ok=True)
    fresh = LocalCatalog(tmp_path, "pinecone", "k")
    assert fresh.get(SPEC.name) is None  # orphaned, rebuildable: the next ensure records it again
    fresh.ensure(SPEC)
    assert fresh.get(SPEC.name) is not None


@pytest.mark.parametrize("bad", ["../x", "Pine", "a b", "", "a/b"])
def test_local_catalog_rejects_bad_type_names(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        LocalCatalog(tmp_path, bad, "k")


def test_local_catalog_concurrent_across_separate_handles(tmp_path: Path) -> None:
    """Separate LocalCatalog objects on one file stand in for separate processes."""
    entries: list[str] = []
    errors: list[BaseException] = []
    start = threading.Barrier(6)

    def work() -> None:
        cat: LocalCatalog | None = None
        try:
            cat = LocalCatalog(tmp_path, "pinecone", "k")
            start.wait(timeout=30)
            entries.append(cat.ensure(SPEC, {"q": 1}).to_json())
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            if cat is not None:
                cat.close()

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert len(entries) == 6 and len(set(entries)) == 1


# ---- concurrent ensure_collection on a store built on a catalog ------------------------------


@pytest.mark.parametrize("make", [_in_store, _local], ids=["in_store", "local"])
def test_concurrent_ensure_collection_on_a_store(tmp_path: Path, make: Callable[[Path], Catalog]) -> None:
    store = MemoryStore(make(tmp_path), options={"quantization": "none"})
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def work() -> None:
        try:
            start.wait()
            store.ensure_collection(SPEC)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    info = store.collection_info(SPEC.name)
    assert info is not None and (info.dim, info.metric, info.count) == (8, "cosine", 0)


@pytest.mark.parametrize("make", [_in_store, _local], ids=["in_store", "local"])
def test_concurrent_ensure_collection_conflict_yields_mismatch(tmp_path: Path, make: Callable[[Path], Catalog]) -> None:
    store = MemoryStore(make(tmp_path))
    results: list[Any] = []
    start = threading.Barrier(4)

    def work(dim: int) -> None:
        start.wait()
        try:
            store.ensure_collection(CollectionSpec(SPEC.name, dim, "cosine"))
            results.append(dim)
        except CollectionMismatch:
            results.append("mismatch")

    threads = [threading.Thread(target=work, args=(d,)) for d in (4, 4, 5, 5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    info = store.collection_info(SPEC.name)
    assert info is not None
    assert sorted(map(str, results)) == sorted(str(info.dim) if d == info.dim else "mismatch" for d in (4, 4, 5, 5))
