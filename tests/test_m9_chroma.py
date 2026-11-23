from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import pytest

pytest.importorskip("chromadb")

from pydantic import ValidationError

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, Record, StoreError, VectorStore
from ragcentre.stores.chroma import (
    CLOUD_MAX_BATCH,
    ChromaConfig,
    ChromaStore,
    _auth_headers,
    translate_filter,
)
from ragcentre.stores.registry import vector_store_types
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract


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
    assert (tmp_path / "stores" / "chroma").is_dir()


# ---- V4.3: tenant/database, modes, auth header, HNSW options, prefix -------------------------


class TestChromaConfiguredContract(VectorStoreContract):
    """The contract suite with every new option set (HNSW, prefix, tenant and database)."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[ChromaStore]:
        s = ChromaStore.persistent(
            tmp_path / "stores" / "chroma",
            tenant="acme",
            database="prod",
            prefix="ragc.",
            hnsw_max_neighbors=24,
            hnsw_ef_construction=150,
            ef_search=80,
        )
        yield s
        s.close()


class TestChromaCloudBatchContract(VectorStoreContract):
    """An instance with the Chroma Cloud batch limit (300) still passes, including multi-batch upserts."""

    @pytest.fixture
    def store(self, ephemeral: ChromaStore) -> ChromaStore:
        return ChromaStore(ephemeral._client, max_batch=CLOUD_MAX_BATCH)


class TestChromaOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"hnsw_max_neighbors": 16, "hnsw_ef_construction": 100}
    options_b: ClassVar[dict[str, Any]] = {"hnsw_max_neighbors": 32, "hnsw_ef_construction": 100}

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> ChromaStore:
        return ChromaStore.persistent(tmp_path / "stores" / "chroma", **options)


class TestChromaEfConstructionOption(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"hnsw_ef_construction": 100}
    options_b: ClassVar[dict[str, Any]] = {"hnsw_ef_construction": 200}

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> ChromaStore:
        return ChromaStore.persistent(tmp_path / "stores" / "chroma", **options)


def test_immutable_options_are_recorded_in_collection_metadata(tmp_path: Path) -> None:
    s = ChromaStore.persistent(tmp_path / "c", hnsw_max_neighbors=24, hnsw_ef_construction=150, ef_search=80)
    s.ensure_collection(CollectionSpec(name="rc_meta_0001", dim=4, metric="cosine", fields={"lang": "str"}))
    col = s._get("rc_meta_0001")
    assert col is not None
    # The pre-existing keys are intact, and the immutable options sit beside them.
    assert col.metadata["rc_dim"] == 4
    assert json.loads(col.metadata["rc_fields"]) == {"lang": "str"}
    assert col.metadata["rc_hnsw_max_neighbors"] == 24
    assert col.metadata["rc_hnsw_ef_construction"] == 150
    hnsw = col.configuration["hnsw"]
    assert (hnsw["max_neighbors"], hnsw["ef_construction"], hnsw["ef_search"]) == (24, 150, 80)
    assert hnsw["space"] == "cosine"


def test_unset_options_record_nothing_and_match_each_other(tmp_path: Path) -> None:
    s = ChromaStore.persistent(tmp_path / "c")
    s.ensure_collection(CollectionSpec(name="rc_meta_0001", dim=4, metric="cosine"))
    col = s._get("rc_meta_0001")
    assert col is not None
    assert "rc_hnsw_max_neighbors" not in col.metadata and "rc_hnsw_ef_construction" not in col.metadata
    s.ensure_collection(CollectionSpec(name="rc_meta_0001", dim=4, metric="cosine"))


def test_collection_built_before_v4_still_opens_and_queries(tmp_path: Path) -> None:
    """A collection written by the old code path (no rc_hnsw_* keys) opens with no options set."""
    chromadb = pytest.importorskip("chromadb")
    path = tmp_path / "stores" / "chroma"
    # Chroma caches an embedded system per path and needs identical settings, so match the adapter's.
    old = chromadb.PersistentClient(path=str(path), settings=chromadb.config.Settings(anonymized_telemetry=False))
    old.get_or_create_collection(
        "rc_legacy_0001",
        configuration={"hnsw": {"space": "cosine"}},
        metadata={"rc_dim": 2, "rc_fields": json.dumps({"n": "int"})},
        embedding_function=None,
    ).upsert(ids=["a", "b"], embeddings=[[1.0, 0.0], [0.0, 1.0]], metadatas=[{"n": 1}, {"n": 2}])
    del old

    s = ChromaStore.persistent(path)
    s.ensure_collection(CollectionSpec(name="rc_legacy_0001", dim=2, metric="cosine", fields={"n": "int"}))
    info = s.collection_info("rc_legacy_0001")
    assert info is not None and (info.dim, info.metric, info.count) == (2, "cosine", 2)
    got = s.query("rc_legacy_0001", [1.0, 0.0], 1, parse_filter({"n": {"$gte": 1}}, {"n": "int"}))  # type: ignore[arg-type]
    assert [m.id for m in got] == ["a"]
    # Asking for options on a collection that was built without them is a mismatch (Decision 5).
    tuned = ChromaStore.persistent(path, hnsw_max_neighbors=32)
    with pytest.raises(CollectionMismatch):
        tuned.ensure_collection(CollectionSpec(name="rc_legacy_0001", dim=2, metric="cosine", fields={"n": "int"}))


def test_ef_search_is_mutable_and_applied_on_ensure(tmp_path: Path) -> None:
    path = tmp_path / "stores" / "chroma"
    spec = CollectionSpec(name="rc_ef_0001", dim=2, metric="l2")
    ChromaStore.persistent(path, ef_search=50).ensure_collection(spec)
    ChromaStore.persistent(path, ef_search=200).ensure_collection(spec)  # no CollectionMismatch
    col = ChromaStore.persistent(path)._get("rc_ef_0001")
    assert col is not None
    assert col.configuration["hnsw"]["ef_search"] == 200


def test_prefix_maps_to_the_physical_name_and_isolates(ephemeral: ChromaStore) -> None:
    a = ChromaStore(ephemeral._client, prefix="aaa_")
    b = ChromaStore(ephemeral._client, prefix="bbb_")
    spec = CollectionSpec(name="rc_pfx_0001", dim=2, metric="cosine")
    a.ensure_collection(spec)
    assert [c.name for c in ephemeral._client.list_collections()] == ["aaa_rc_pfx_0001"]
    assert a.collection_info("rc_pfx_0001") is not None
    assert b.collection_info("rc_pfx_0001") is None
    b.ensure_collection(CollectionSpec(name="rc_pfx_0001", dim=3, metric="l2"))  # no clash with a's collection
    a.drop_collection("rc_pfx_0001")
    assert a.collection_info("rc_pfx_0001") is None and b.collection_info("rc_pfx_0001") is not None


def test_prefix_must_still_fit_chroma_naming(ephemeral: ChromaStore) -> None:
    too_long = ChromaStore(ephemeral._client, prefix="p" * 60)
    with pytest.raises(StoreError, match="3-63"):
        too_long.ensure_collection(CollectionSpec(name="rc_pfx_0001", dim=2, metric="cosine"))
    with pytest.raises(ValidationError):
        ChromaConfig.model_validate({"prefix": "bad prefix!"})
    with pytest.raises(ValidationError):
        ChromaConfig.model_validate({"prefix": "a..b"})


def test_tenant_and_database_are_created_and_isolate(tmp_path: Path) -> None:
    path = tmp_path / "stores" / "chroma"
    spec = CollectionSpec(name="rc_ten_0001", dim=2, metric="cosine")
    prod = ChromaStore.persistent(path, tenant="acme", database="prod")
    prod.ensure_collection(spec)
    prod.upsert("rc_ten_0001", [Record("a", [1.0, 0.0], {})])
    stage = ChromaStore.persistent(path, tenant="acme", database="stage")
    assert stage.collection_info("rc_ten_0001") is None
    default = ChromaStore.persistent(path)
    assert default.collection_info("rc_ten_0001") is None
    again = ChromaStore.persistent(path, tenant="acme", database="prod")  # reopening an existing tenant/database
    info = again.collection_info("rc_ten_0001")
    assert info is not None and info.count == 1


def test_auth_header_styles() -> None:
    assert _auth_headers("Authorization", "tok") == {"Authorization": "Bearer tok"}
    assert _auth_headers("authorization", "tok") == {"authorization": "Bearer tok"}
    assert _auth_headers("x-chroma-token", "tok") == {"x-chroma-token": "tok"}
    assert _auth_headers("X-Proxy-Auth", "tok") == {"X-Proxy-Auth": "tok"}
    assert _auth_headers("Authorization", None) is None
    assert _auth_headers("Authorization", "") is None


def _factory(config: dict[str, Any], secrets: dict[str, str], tmp_path: Path) -> ChromaStore:
    t = vector_store_types()["chroma"]
    store = t.factory(t.config_model.model_validate(config), secrets, tmp_path)
    assert isinstance(store, ChromaStore)
    return store


def test_cloud_mode_builds_a_cloud_client_with_batch_300(tmp_path: Path) -> None:
    fake = mock.MagicMock()
    fake.get_max_batch_size.return_value = 5461
    with mock.patch("chromadb.CloudClient", return_value=fake) as cloud:
        store = _factory(
            {"mode": "cloud", "tenant": "t-1", "database": "db-1", "cloud_host": "chroma.example", "ef_search": 70},
            {"api_key": "ck-123"},
            tmp_path,
        )
    kwargs = cloud.call_args.kwargs
    assert (kwargs["api_key"], kwargs["tenant"], kwargs["database"], kwargs["cloud_host"]) == (
        "ck-123", "t-1", "db-1", "chroma.example",
    )  # fmt: skip
    assert store.capabilities.max_batch == CLOUD_MAX_BATCH == 300
    assert store.client_max_batch == 300
    # The type's listing keeps the best case.
    assert vector_store_types()["chroma"].capabilities.max_batch == 1000


def test_cloud_mode_needs_the_api_key(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="api_key"):
        _factory({"mode": "cloud", "tenant": "t", "database": "d"}, {}, tmp_path)


def test_cloud_upserts_are_split_at_300(tmp_path: Path) -> None:
    fake = mock.MagicMock()
    with mock.patch("chromadb.CloudClient", return_value=fake):
        store = _factory({"mode": "cloud", "tenant": "t", "database": "d"}, {"api_key": "k"}, tmp_path)
    col = fake.get_collection.return_value
    col.metadata = {"rc_dim": 2, "rc_fields": "{}"}
    col.configuration = {"hnsw": {"space": "cosine"}}
    store.upsert("rc_cloud_0001", [Record(f"r{i}", [1.0, 0.0], {}) for i in range(300)])
    assert col.upsert.call_count == 1
    with pytest.raises(StoreError, match="exceeds max_batch"):
        store.upsert("rc_cloud_0001", [Record(f"r{i}", [1.0, 0.0], {}) for i in range(301)])


@pytest.mark.parametrize(
    ("config", "secrets", "expected"),
    [
        ({"host": "h"}, {"token": "tok"}, {"Authorization": "Bearer tok"}),
        ({"host": "h", "token_header": "x-chroma-token"}, {"token": "tok"}, {"x-chroma-token": "tok"}),
        ({"host": "h"}, {}, None),
    ],
)
def test_http_mode_headers_and_tenant(
    tmp_path: Path, config: dict[str, Any], secrets: dict[str, str], expected: dict[str, str] | None
) -> None:
    fake = mock.MagicMock()
    fake.get_max_batch_size.return_value = 5461
    with mock.patch("chromadb.HttpClient", return_value=fake) as http:
        store = _factory({**config, "port": 9000, "ssl": True, "tenant": "t", "database": "d"}, secrets, tmp_path)
    kwargs = http.call_args.kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["ssl"]) == ("h", 9000, True)
    assert kwargs["headers"] == expected
    assert (kwargs["tenant"], kwargs["database"]) == ("t", "d")
    assert store.capabilities.max_batch == 1000


def test_local_mode_uses_a_persistent_client(tmp_path: Path) -> None:
    store = _factory({"mode": "local"}, {}, tmp_path)
    store.ensure_collection(CollectionSpec(name="rc_loc_0001", dim=2, metric="cosine"))
    assert (tmp_path / "stores" / "chroma").is_dir()
    store2 = _factory({"path": "sub/dir"}, {}, tmp_path)  # relative paths live under the data dir
    store2.healthcheck()
    assert (tmp_path / "stores" / "sub" / "dir").is_dir()


def test_config_modes_and_backward_compatible_inference() -> None:
    assert ChromaConfig.model_validate({}).mode == "local"
    assert ChromaConfig.model_validate({"host": "h"}).mode == "http"  # pre-V4 provider rows
    assert ChromaConfig.model_validate({"path": "p"}).mode == "local"
    assert ChromaConfig.model_validate({"mode": "cloud"}).mode == "cloud"
    assert ChromaConfig.model_validate({}).token_header == "Authorization"
    for bad in (
        {"mode": "cloud", "host": "h"},
        {"mode": "cloud", "path": "p"},
        {"mode": "http"},
        {"mode": "http", "host": "h", "path": "p"},
        {"mode": "local", "host": "h"},
        {"mode": "nope"},
        {"token_header": "  "},
        {"hnsw_max_neighbors": 0},
        {"hnsw_ef_construction": 0},
        {"ef_search": 0},
        {"surprise": 1},
    ):
        with pytest.raises(ValidationError):
            ChromaConfig.model_validate(bad)


def test_registry_entry_declares_immutable_fields_and_secrets() -> None:
    t = vector_store_types()["chroma"]
    assert t.immutable_fields == ("hnsw_max_neighbors", "hnsw_ef_construction")
    assert set(t.secret_keys) == {"token", "api_key"}
    assert "ef_search" not in t.immutable_fields


def test_index_shaping_fields_are_immutable_through_the_api(client: Any) -> None:
    """hnsw_* return 409 on update; connection and query-time fields stay mutable."""
    made = client.post(
        "/v1/providers",
        json={
            "name": "chroma-immutable",
            "kind": "vector_store",
            "type": "chroma",
            "config": {"mode": "http", "host": "chroma1", "hnsw_max_neighbors": 16, "ef_search": 40},
        },
    )
    assert made.status_code == 201, made.text
    url = f"/v1/providers/{made.json()['id']}"
    for field, value in (("hnsw_max_neighbors", 32), ("hnsw_ef_construction", 128)):
        refused = client.patch(url, json={"config": {field: value}})
        assert refused.status_code == 409, (field, refused.text)
        assert refused.json()["error"]["code"] == "immutable_field"
        assert refused.json()["error"]["details"] == {"field": field}
    accepted = client.patch(
        url, json={"config": {"mode": "http", "host": "chroma2", "hnsw_max_neighbors": 16, "ef_search": 120}}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["config"]["ef_search"] == 120
    assert accepted.json()["config"]["host"] == "chroma2"


def test_connection_failures_become_store_errors(tmp_path: Path) -> None:
    with (
        mock.patch("chromadb.HttpClient", side_effect=ValueError("Tenant [x] not found")),
        pytest.raises(StoreError, match=r"tenant='x'.*must be created first"),
    ):
        _factory({"mode": "http", "host": "h", "tenant": "x"}, {}, tmp_path)
    with (
        mock.patch("chromadb.CloudClient", side_effect=RuntimeError("Permission denied.")),
        pytest.raises(StoreError, match=r"Chroma Cloud.*Permission denied"),
    ):
        _factory({"mode": "cloud", "tenant": "t", "database": "d"}, {"api_key": "bad"}, tmp_path)
