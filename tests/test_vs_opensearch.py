"""OpenSearch store: the contract suite against a live server (faiss and lucene engines), plus config,
catalog and filter-compiler tests that need no server.

Live tests need a running server with the security plugin off: ``docker run -p 59201:9200
-e discovery.type=single-node -e DISABLE_SECURITY_PLUGIN=true -e DISABLE_INSTALL_DEMO_CONFIG=true
-e OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m" opensearchproject/opensearch:3.10.0``. They skip when the
``opensearch`` extra is missing or no server answers. Override the location with ``RAGCENTRE_TEST_OPENSEARCH_URL``.
Each test uses its own index prefix and drops what it made.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

import ragcentre.stores.opensearch as os_module
from ragcentre.models.filters import Cond, parse_filter
from ragcentre.stores.base import CollectionSpec, Record, StoreError, StoreUnavailable
from ragcentre.stores.opensearch import STORE_TYPE, OpenSearchConfig, OpenSearchStore
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract

HAVE_CLIENT = importlib.util.find_spec("opensearchpy") is not None
URL = os.environ.get("RAGCENTRE_TEST_OPENSEARCH_URL", "http://localhost:59201")


@pytest.fixture(scope="module")
def server() -> None:
    if not HAVE_CLIENT:
        pytest.skip("the 'opensearch' extra is not installed")
    try:
        OpenSearchStore.connect(url=URL, request_timeout=5).healthcheck()
    except StoreError as exc:
        pytest.skip(f"no OpenSearch server at {URL} ({str(exc).splitlines()[0][:120]})")


def _prefix() -> str:
    return f"t{uuid.uuid4().hex[:10]}_"


def _drop_prefixed(prefix: str) -> None:
    store = OpenSearchStore.connect(url=URL)
    for n in store._client.indices.get(index="*", expand_wildcards="open"):
        if n.startswith(prefix):
            store._client.indices.delete(index=n, ignore_unavailable=True)
    store.close()


@pytest.fixture
def prefix(server: None) -> Iterator[str]:
    p = _prefix()
    yield p
    _drop_prefixed(p)


class TestOpenSearchFaissContract(VectorStoreContract):
    @pytest.fixture
    def store(self, prefix: str) -> Iterator[OpenSearchStore]:
        s = OpenSearchStore.connect(url=URL, prefix=prefix, engine="faiss")
        yield s
        s.close()


class TestOpenSearchLuceneContract(VectorStoreContract):
    @pytest.fixture
    def store(self, prefix: str) -> Iterator[OpenSearchStore]:
        s = OpenSearchStore.connect(url=URL, prefix=prefix, engine="lucene")
        yield s
        s.close()


def _prefix_for(path: Path) -> str:
    return f"t{uuid.uuid5(uuid.NAMESPACE_URL, str(path)).hex[:10]}_"


class TestOpenSearchEngineOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"engine": "faiss"}
    options_b: ClassVar[dict[str, Any]] = {"engine": "lucene"}

    @pytest.fixture(autouse=True)
    def _cleanup(self, server: None, tmp_path: Path) -> Iterator[None]:
        yield
        _drop_prefixed(_prefix_for(tmp_path))

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> OpenSearchStore:
        return OpenSearchStore.connect(url=URL, prefix=_prefix_for(tmp_path), **options)


# ---- no server needed: config and registration ------------------------------------------------


def test_client_module_is_not_shadowed_by_the_store_module() -> None:
    if not HAVE_CLIENT:
        pytest.skip("the 'opensearch' extra is not installed")
    import importlib

    client = importlib.import_module("opensearchpy")
    assert hasattr(client, "OpenSearch")
    assert "ragcentre" not in str(client.__file__)
    assert os_module.__name__ == "ragcentre.stores.opensearch"


def test_config_defaults_and_validation() -> None:
    config = OpenSearchConfig(url="http://x:9200")
    assert (config.engine, config.prefix, config.num_candidates_factor) == ("faiss", "", 10)
    with pytest.raises(ValidationError):
        OpenSearchConfig()  # type: ignore[call-arg]
    for bad in ("localhost:9200", "ftp://x", "http://"):
        with pytest.raises(ValidationError, match="http"):
            OpenSearchConfig(url=bad)
    with pytest.raises(ValidationError):
        OpenSearchConfig(url="http://x:9200", bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        OpenSearchConfig(url="http://x:9200", prefix="Upper")
    with pytest.raises(ValidationError):
        OpenSearchConfig(url="http://x:9200", num_candidates_factor=0)


@pytest.mark.parametrize("engine", ["nmslib", "NMSLIB", "", "annoy"])
def test_nmslib_and_unknown_engines_are_never_accepted(engine: str) -> None:
    with pytest.raises(ValidationError):
        OpenSearchConfig(url="http://x:9200", engine=engine)  # type: ignore[arg-type]
    with pytest.raises(StoreError, match="faiss"):
        OpenSearchStore(RecordingClient(), engine=engine)  # type: ignore[arg-type]


def test_store_type_registration() -> None:
    assert STORE_TYPE.type == "opensearch"
    assert STORE_TYPE.immutable_fields == ("engine",)
    assert STORE_TYPE.secret_keys == ("password",)
    assert STORE_TYPE.capabilities.max_dim == 16_000
    assert STORE_TYPE.capabilities.metrics == {"cosine", "l2", "dot"}


@pytest.mark.skipif(HAVE_CLIENT, reason="only meaningful when the extra is absent")
def test_missing_extra_is_a_clear_error() -> None:
    with pytest.raises(StoreUnavailable, match="opensearch"):
        OpenSearchStore.connect(url="http://x:9200")


# ---- no server needed: the requests the OpenSearch store sends ---------------------------------


class RecordingIndices:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.existing: dict[str, Any] = {}

    def get_mapping(self, index: str) -> Any:
        if index not in self.existing:
            err = StoreError("missing")
            err.status_code = 404  # type: ignore[attr-defined]
            raise err
        return {index: {"mappings": self.existing[index]}}

    def create(self, index: str, body: dict[str, Any]) -> None:
        self.created.append({"index": index, "body": body})
        self.existing[index] = body["mappings"]

    def put_mapping(self, index: str, body: dict[str, Any]) -> None:
        self.existing[index]["properties"].update(body["properties"])
        self.existing[index]["_meta"] = body["_meta"]


class RecordingClient:
    def __init__(self, hits: list[dict[str, Any]] | None = None) -> None:
        self.indices = RecordingIndices()
        self.searches: list[dict[str, Any]] = []
        self.bulks: list[dict[str, Any]] = []
        self.hits = hits or []

    def info(self) -> dict[str, Any]:
        return {"version": {"number": "3.10.0"}}

    def bulk(self, body: list[Any], refresh: str) -> dict[str, Any]:
        self.bulks.append({"body": body, "refresh": refresh})
        return {"errors": False, "items": []}

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.searches.append({"index": index, "body": body})
        return {"hits": {"hits": self.hits}}

    def close(self) -> None:
        pass


NAME = "rc_os_0001"


@pytest.mark.parametrize("engine", ["faiss", "lucene"])
@pytest.mark.parametrize(("metric", "space"), [("cosine", "cosinesimil"), ("l2", "l2"), ("dot", "innerproduct")])
def test_index_request_shape(engine: str, metric: str, space: str) -> None:
    client = RecordingClient()
    store = OpenSearchStore(client, prefix="p_", engine=engine)  # type: ignore[arg-type]
    store.ensure_collection(CollectionSpec(NAME, 12, metric, {"year": "int"}))  # type: ignore[arg-type]
    (created,) = client.indices.created
    assert created["index"] == "p_" + NAME
    assert created["body"]["settings"] == {"index": {"knn": True}}
    mappings = created["body"]["mappings"]
    assert mappings["properties"]["_rc_vector"] == {
        "type": "knn_vector", "dimension": 12,
        "method": {"name": "hnsw", "engine": engine, "space_type": space},
    }  # fmt: skip
    assert "nmslib" not in str(created)
    assert mappings["_meta"]["ragcentre"]["options"] == {"engine": engine}
    assert mappings["properties"]["year"] == {"type": "long"}


def test_old_servers_are_refused_with_a_clear_message() -> None:
    client = RecordingClient()
    client.info = lambda: {"version": {"number": "2.11.0"}}  # type: ignore[method-assign]
    with pytest.raises(StoreError, match=r"too old.*2\.19"):
        OpenSearchStore(client).ensure_collection(CollectionSpec(NAME, 4, "cosine"))
    with pytest.raises(StoreError, match=r"too old.*2\.13"):
        OpenSearchStore(client, engine="lucene").ensure_collection(CollectionSpec(NAME, 4, "cosine"))


def test_bulk_waits_for_refresh() -> None:
    client = RecordingClient()
    store = OpenSearchStore(client)
    store.ensure_collection(CollectionSpec(NAME, 4, "cosine"))
    store.upsert(NAME, [Record(str(uuid.uuid4()), [1.0, 0.0, 0.0, 0.0], {})])
    store.delete(NAME, ["a"])
    assert [b["refresh"] for b in client.bulks] == ["wait_for", "wait_for"]


def test_faiss_query_filters_inside_knn_and_sets_ef_search() -> None:
    hits = [{"_id": "a", "_score": 0.9}, {"_id": "b", "_score": 0.5}]
    client = RecordingClient(hits)
    store = OpenSearchStore(client, num_candidates_factor=7)
    store.ensure_collection(CollectionSpec(NAME, 4, "cosine", {"lang": "str"}))
    got = store.query(NAME, [1, 0, 0, 0], 5, Cond("lang", "$ne", "en"))
    (search,) = client.searches
    body = search["body"]
    assert set(body["query"]) == {"knn"}  # no bool/post_filter around it
    clause = body["query"]["knn"]["_rc_vector"]
    assert clause["filter"] == {"bool": {"must_not": [{"term": {"lang": {"value": "en"}}}]}}
    assert (clause["k"], body["size"]) == (5, 5)
    assert clause["method_parameters"] == {"ef_search": 35}
    assert body["_source"] is False
    assert [(m.id, round(m.score, 6)) for m in got] == [("a", 0.8), ("b", 0.0)]


def test_lucene_query_sends_no_ef_search() -> None:
    client = RecordingClient()
    store = OpenSearchStore(client, engine="lucene")
    store.ensure_collection(CollectionSpec(NAME, 4, "l2"))
    store.query(NAME, [1, 0, 0, 0], 3)
    clause = client.searches[0]["body"]["query"]["knn"]["_rc_vector"]
    assert "method_parameters" not in clause and "filter" not in clause


# ---- live: behaviour the contract suite does not reach ----------------------------------------


@pytest.mark.parametrize("engine", ["faiss", "lucene"])
class TestOpenSearchLive:
    NAME = "rc_live_0001"

    @pytest.fixture
    def store(self, prefix: str, engine: str) -> Iterator[OpenSearchStore]:
        s = OpenSearchStore.connect(url=URL, prefix=prefix, engine=engine)  # type: ignore[arg-type]
        yield s
        s.close()

    def test_writes_are_visible_at_once(self, store: OpenSearchStore) -> None:
        store.ensure_collection(CollectionSpec(self.NAME, 4, "cosine", {"lang": "str"}))
        rid = str(uuid.uuid4())
        store.upsert(self.NAME, [Record(rid, [1.0, 0.0, 0.0, 0.0], {"document_id": "d", "lang": "en"})])
        info = store.collection_info(self.NAME)
        assert info is not None and info.count == 1
        assert [m.id for m in store.query(self.NAME, [1.0, 0.0, 0.0, 0.0], 1)] == [rid]
        store.delete(self.NAME, [rid])
        info = store.collection_info(self.NAME)
        assert info is not None and info.count == 0
        assert store.capabilities.consistency == "immediate"

    def test_missing_field_semantics_on_the_server(self, store: OpenSearchStore) -> None:
        store.ensure_collection(CollectionSpec(self.NAME, 4, "dot", {"lang": "str", "year": "int"}))
        with_all, no_year, no_lang = (str(uuid.uuid4()) for _ in range(3))
        store.upsert(
            self.NAME,
            [
                Record(with_all, [1.0, 0.0, 0.0, 0.0], {"document_id": "a", "lang": "en", "year": 2020}),
                Record(no_year, [0.9, 0.0, 0.0, 0.0], {"document_id": "b", "lang": "en"}),
                Record(no_lang, [0.8, 0.0, 0.0, 0.0], {"document_id": "c", "year": 2021}),
            ],
        )
        schema = {"lang": "str", "year": "int", "document_id": "str"}

        def ids(raw: dict[str, Any]) -> set[str]:
            f = parse_filter(raw, schema)  # type: ignore[arg-type]
            return {m.id for m in store.query(self.NAME, [1.0, 0.0, 0.0, 0.0], 10, f)}

        assert ids({"year": {"$ne": 2020}}) == {no_year, no_lang}
        assert ids({"year": {"$nin": [2020]}}) == {no_year, no_lang}
        assert ids({"lang": {"$ne": "fr"}}) == {with_all, no_year, no_lang}
        assert ids({"year": {"$gte": 2000}}) == {with_all, no_lang}
        assert ids({"$or": [{"lang": "en"}, {"year": {"$gt": 2020}}]}) == {with_all, no_year, no_lang}

    def test_k_equal_to_a_larger_collection_returns_everything(self, store: OpenSearchStore) -> None:
        """Faiss searches ef_search candidates (default 100): k above that must still be filled."""
        import random

        rng = random.Random(4)
        store.ensure_collection(CollectionSpec(self.NAME, 4, "l2"))
        recs = [Record(str(uuid.uuid4()), [rng.random() for _ in range(4)], {}) for _ in range(400)]
        for i in range(0, 400, 200):
            store.upsert(self.NAME, recs[i : i + 200])
        assert len(store.query(self.NAME, [0.5] * 4, 400)) == 400
        assert len(store.query(self.NAME, [0.5] * 4, 300)) == 300

    def test_max_dim_matches_the_server(self, store: OpenSearchStore) -> None:
        assert store.capabilities.max_dim == 16_000
        store.ensure_collection(CollectionSpec(self.NAME, 16_000, "cosine"))  # the largest the server accepts
        with pytest.raises(StoreError, match="16000"):
            store.ensure_collection(CollectionSpec("rc_live_0002", 16_001, "cosine"))

    def test_zero_vector_with_cosine_is_a_clear_error(self, store: OpenSearchStore) -> None:
        store.ensure_collection(CollectionSpec(self.NAME, 4, "cosine"))
        with pytest.raises(StoreError, match="zero vector"):
            store.upsert(self.NAME, [Record(str(uuid.uuid4()), [0.0] * 4, {})])

    def test_foreign_index_is_not_adopted(self, store: OpenSearchStore) -> None:
        store._client.indices.create(
            index=store._physical(self.NAME), body={"mappings": {"properties": {"t": {"type": "text"}}}}
        )
        with pytest.raises(StoreError, match="not created by RAGCentre"):
            store.ensure_collection(CollectionSpec(self.NAME, 4, "cosine"))

    def test_catalog_survives_a_new_client(self, store: OpenSearchStore, prefix: str, engine: str) -> None:
        store.ensure_collection(CollectionSpec(self.NAME, 4, "dot", {"lang": "str"}))
        again = OpenSearchStore.connect(url=URL, prefix=prefix, engine=engine)  # type: ignore[arg-type]
        info = again.collection_info(self.NAME)
        assert info is not None and (info.dim, info.metric) == (4, "dot")
        again.ensure_collection(CollectionSpec(self.NAME, 4, "dot", {"lang": "str", "year": "int"}))
        mapping = again._client.indices.get_mapping(index=again._physical(self.NAME))[again._physical(self.NAME)][
            "mappings"
        ]
        assert mapping["_meta"]["ragcentre"]["fields"] == {"document_id": "str", "lang": "str", "year": "int"}
        again.close()
