"""Catalog concurrency, numeric precision and SDK policy regressions from the final review."""

from __future__ import annotations

from typing import Any

import pytest

from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import Record, StoreError
from ragcentre.stores.elasticsearch import ElasticsearchStore
from ragcentre.stores.mongodb import MongoDBStore, _scalar, compile_filter
from ragcentre.stores.opensearch import OpenSearchStore
from test_vs_elasticsearch import NAME, FakeEngineStore, spec
from test_vs_mongodb import FakeClient, _spec


def test_search_catalog_preserves_concurrent_field_additions(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = FakeEngineStore(), FakeEngineStore()
    first.ensure_collection(spec(fields={"lang": "str"}))
    second.indices, second.docs = first.indices, first.docs
    original = first._put_mappings

    def interleaved(index: str, mapping: dict[str, Any]) -> None:
        # First has already read the old catalog. Second completes its addition before
        # first replaces _meta with a snapshot that lacks second's field.
        second.ensure_collection(spec(fields={"score": "float"}))
        original(index, mapping)

    monkeypatch.setattr(first, "_put_mappings", interleaved)
    first.ensure_collection(spec(fields={"year": "int"}))
    for store in (first, second):
        assert store._entry(NAME).fields == {"document_id": "str", "lang": "str", "year": "int", "score": "float"}
    first.upsert(
        NAME, [Record("00000000-0000-0000-0000-000000000001", [1.0, 0.0, 0.0, 0.0], {"score": 0.5, "year": 2020})]
    )
    assert first.docs[NAME]["00000000-0000-0000-0000-000000000001"]["score"] == 0.5


@pytest.mark.parametrize("value", [2**53 + 1, -(2**53 + 1)])
def test_mongodb_rejects_inexact_integer_filters_and_metadata(value: int) -> None:
    store = MongoDBStore(FakeClient(), database="d")
    try:
        store.ensure_collection(_spec())
        entry = store._entry("rc_fake_0001")
        with pytest.raises(StoreError, match="2\\*\\*53"):
            store._document(Record("id", [1.0, 0.0, 0.0, 0.0], {"year": value}), entry)
        for op, operand in (("$eq", value), ("$in", [value]), ("$gt", value)):
            f = parse_filter({"year": {op: operand}}, {"year": "int"})
            assert f is not None
            with pytest.raises(StoreError, match="2\\*\\*53"):
                compile_filter(f, {"year": "int"})
    finally:
        store.close()
    assert _scalar(2**53) == 2**53
    assert _scalar(-(2**53)) == -(2**53)
    assert _scalar(True) is True


def test_quantized_elasticsearch_requires_full_precision_rescoring() -> None:
    store = ElasticsearchStore(object(), index_options="int8_hnsw")
    store._server_version = (8, 17, 1)
    with pytest.raises(StoreError, match=r"8\.18"):
        store._check_server({"index_options": "int8_hnsw"}, "cosine")
    store._server_version = (8, 18, 0)
    store._check_server({"index_options": "int8_hnsw"}, "cosine")


def test_existing_quantized_elasticsearch_query_rejects_missing_rescoring() -> None:
    from ragcentre.stores.catalog import CatalogEntry

    class Client:
        def search(self, **kwargs: Any) -> Any:
            pytest.fail("unsupported quantized search must fail before contacting the server")

    # The provider's current option can differ from the existing collection's recorded option.
    store = ElasticsearchStore(Client(), index_options="hnsw")
    store._server_version = (8, 17, 0)
    entry = CatalogEntry(4, "cosine", {}, {"index_options": "int8_hnsw"})
    with pytest.raises(StoreError, match=r"8\.18 or later"):
        store._knn("existing", entry, [1.0, 0.0, 0.0, 0.0], 1, None)


def test_elasticsearch_disables_sdk_retries() -> None:
    pytest.importorskip("elasticsearch")
    store = ElasticsearchStore.connect(url="http://localhost:1")
    try:
        assert store._client._max_retries == 0
        assert store._client._retry_on_timeout is False
    finally:
        store.close()


def test_opensearch_disables_sdk_retries() -> None:
    pytest.importorskip("opensearchpy")
    store = OpenSearchStore.connect(url="http://localhost:1")
    try:
        assert store._client.transport.max_retries == 0
        assert store._client.transport.retry_on_timeout is False
    finally:
        store.close()


def test_mongodb_counts_searchable_records_when_numdocs_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_vs_mongodb import _recs

    store = MongoDBStore(FakeClient(), database="d", exact=True)
    try:
        store.ensure_collection(_spec())
        store.upsert("rc_fake_0001", _recs(3))
        original = store._index

        def without_count(physical: str) -> dict[str, Any]:
            index = dict(original(physical) or {})
            index.pop("numDocs", None)
            return index

        monkeypatch.setattr(store, "_index", without_count)
        monkeypatch.setattr(store, "_visible", lambda *args: 2)
        info = store.collection_info("rc_fake_0001")
        assert info is not None and info.count == 2
    finally:
        store.close()


def test_mongodb_settle_waits_for_every_insert_even_when_the_canary_is_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    import test_vs_mongodb as module

    store = MongoDBStore(FakeClient(), database="d", exact=True)
    try:
        store._canary = threading.local()
        module._track(store)
        store.ensure_collection(_spec())
        store.upsert("rc_fake_0001", module._recs(3))
        counts = iter([2, 2, 3])  # initial info, first poll, second poll
        monkeypatch.setattr(store, "_visible", lambda *args: next(counts))

        def wait(fn: Any, **kwargs: Any) -> None:
            assert fn() is False  # the last-written record is visible but another insert is not
            assert fn() is True

        monkeypatch.setattr(module, "wait_until", wait)
        module.wait_visible(store, "rc_fake_0001")
    finally:
        store.close()


def test_mongodb_disables_sdk_read_retries() -> None:
    pytest.importorskip("pymongo")
    store = MongoDBStore.connect(uri="mongodb://localhost:1")
    try:
        assert store._client.options.retry_reads is False
    finally:
        store.close()
