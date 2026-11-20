"""Elasticsearch store: the contract suite against a live server, plus config, catalog and
filter-compiler tests that need no server.

Live tests need a running server (security off): ``docker run -p 59200:9200 -e discovery.type=single-node
-e xpack.security.enabled=false -e ES_JAVA_OPTS="-Xms512m -Xmx512m" docker.elastic.co/elasticsearch/elasticsearch:9.5.1``.
They skip when the ``elasticsearch`` extra is missing or no server answers. Override the location with
``RAGCENTRE_TEST_ES_URL``. Each test uses its own index prefix and drops what it made.
"""

from __future__ import annotations

import importlib
import importlib.util
import math
import os
import random
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

import ragcentre.stores.elasticsearch as es_module
from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import And, Cond, Or, matches, parse_filter
from ragcentre.stores._lucene_scores import (
    SearchEngineStore,
    compile_filter,
    compile_query_dsl,
    forward_score,
    invert_score,
)
from ragcentre.stores.base import (
    CollectionNotFound,
    CollectionSpec,
    Metric,
    Record,
    StoreError,
    StoreUnavailable,
)
from ragcentre.stores.catalog import CatalogEntry
from ragcentre.stores.elasticsearch import STORE_TYPE, ElasticsearchConfig, ElasticsearchStore
from ragcentre.testing.random_data import SCHEMA_FIELDS, full_schema, random_filter, random_metadata
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract

HAVE_CLIENT = importlib.util.find_spec("elasticsearch") is not None
needs_client = pytest.mark.skipif(not HAVE_CLIENT, reason="the 'elasticsearch' extra is not installed")
URL = os.environ.get("RAGCENTRE_TEST_ES_URL", "http://localhost:59200")


@pytest.fixture(scope="module")
def server() -> None:
    if not HAVE_CLIENT:
        pytest.skip("the 'elasticsearch' extra is not installed")
    try:
        ElasticsearchStore.connect(url=URL, request_timeout=5).healthcheck()
    except StoreError as exc:
        pytest.skip(f"no Elasticsearch server at {URL} ({str(exc).splitlines()[0][:120]})")


def _prefix() -> str:
    return f"t{uuid.uuid4().hex[:10]}_"


def _drop_prefixed(prefix: str) -> None:
    store = ElasticsearchStore.connect(url=URL)
    names = [n for n in store._client.indices.get(index="*", expand_wildcards="open").body if n.startswith(prefix)]
    for n in names:
        store._client.indices.delete(index=n, ignore_unavailable=True)
    store.close()


@pytest.fixture
def prefix(server: None) -> Iterator[str]:
    p = _prefix()
    yield p
    _drop_prefixed(p)


class TestElasticsearchContract(VectorStoreContract):
    @pytest.fixture
    def store(self, prefix: str) -> Iterator[ElasticsearchStore]:
        s = ElasticsearchStore.connect(url=URL, prefix=prefix)
        yield s
        s.close()


class TestElasticsearchFlatContract(VectorStoreContract):
    @pytest.fixture
    def store(self, prefix: str) -> Iterator[ElasticsearchStore]:
        s = ElasticsearchStore.connect(url=URL, prefix=prefix, index_options="flat")
        yield s
        s.close()


class TestElasticsearchInt8Contract(VectorStoreContract):
    """Quantised vectors: ``rescore_vector`` makes the returned scores exact, so the suite still holds."""

    @pytest.fixture
    def store(self, prefix: str) -> Iterator[ElasticsearchStore]:
        s = ElasticsearchStore.connect(url=URL, prefix=prefix, index_options="int8_hnsw")
        yield s
        s.close()


def _prefix_for(path: Path) -> str:
    return f"t{uuid.uuid5(uuid.NAMESPACE_URL, str(path)).hex[:10]}_"


class TestElasticsearchIndexOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"index_options": "hnsw"}
    options_b: ClassVar[dict[str, Any]] = {"index_options": "flat"}

    @pytest.fixture(autouse=True)
    def _cleanup(self, server: None, tmp_path: Path) -> Iterator[None]:
        yield
        _drop_prefixed(_prefix_for(tmp_path))

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> ElasticsearchStore:
        return ElasticsearchStore.connect(url=URL, prefix=_prefix_for(tmp_path), **options)


# ---- no server needed: scores ---------------------------------------------------------------


@pytest.mark.parametrize("metric", ["cosine", "dot", "l2"])
@pytest.mark.parametrize("value", [-12.5, -3.0, -1.0, -0.25, 0.0, 0.25, 1.0, 3.0, 12.5])
def test_score_inverse_round_trip(metric: Metric, value: float) -> None:
    if metric == "l2":
        value = -abs(value)
    if metric == "cosine":
        value = max(-1.0, min(1.0, value / 12.5))
    assert invert_score(metric, forward_score(metric, value)) == pytest.approx(value, abs=1e-9)


def test_score_inverse_known_values() -> None:
    # Values as returned by Elasticsearch 9.5.1 for these similarities (verified on the live server).
    assert invert_score("cosine", 0.9488818605710343) == pytest.approx(0.8977637211420685)
    assert invert_score("l2", 1 / (1 + 0.9558472440387074**2)) == pytest.approx(-0.9558472440387074)
    assert invert_score("dot", 4.78220791591059) == pytest.approx(3.7822079159105892)  # dot >= 0: s - 1
    assert invert_score("dot", 0.25) == pytest.approx(-3.0)  # dot < 0: s = 1 / (1 - dot)
    assert invert_score("dot", 1.0) == 0.0
    assert invert_score("l2", 1.0) == 0.0 and invert_score("cosine", 0.5) == 0.0


def test_scores_are_monotonic_so_ranking_is_kept() -> None:
    for metric in ("cosine", "dot", "l2"):
        values = [-9.0, -2.0, -0.5, 0.0, 0.5, 2.0, 9.0] if metric == "dot" else [-0.9, -0.5, -0.1, 0.0, 0.5, 0.9]
        if metric == "l2":
            values = [-5.0, -2.0, -0.5, -0.1, 0.0]
        engine = [forward_score(metric, v) for v in values]
        assert engine == sorted(engine)
        assert [invert_score(metric, s) for s in engine] == pytest.approx(values)


# ---- no server needed: filter compiler ------------------------------------------------------


def c(field: str, op: str, value: Any) -> Cond:
    return Cond(field, op, value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cond", "dsl"),
    [
        (c("lang", "$eq", "en"), {"term": {"lang": {"value": "en"}}}),
        (c("lang", "$ne", "en"), {"bool": {"must_not": [{"term": {"lang": {"value": "en"}}}]}}),
        (c("year", "$gt", 2020), {"range": {"year": {"gt": 2020}}}),
        (c("year", "$gte", 2020), {"range": {"year": {"gte": 2020}}}),
        (c("score", "$lt", 0.5), {"range": {"score": {"lt": 0.5}}}),
        (c("score", "$lte", 1), {"range": {"score": {"lte": 1}}}),
        (c("lang", "$in", ("en", "fr")), {"terms": {"lang": ["en", "fr"]}}),
        (c("lang", "$nin", ("en", "fr")), {"bool": {"must_not": [{"terms": {"lang": ["en", "fr"]}}]}}),
        (c("public", "$eq", True), {"term": {"public": {"value": True}}}),
        (c("public", "$ne", False), {"bool": {"must_not": [{"term": {"public": {"value": False}}}]}}),
    ],
)
def test_every_operator_compiles(cond: Cond, dsl: dict[str, Any]) -> None:
    assert compile_query_dsl(cond) == dsl


def test_combinators() -> None:
    a, b = c("a", "$eq", 1), c("b", "$eq", "x")
    assert compile_query_dsl(And((a, b))) == {"bool": {"filter": [compile_query_dsl(a), compile_query_dsl(b)]}}
    assert compile_query_dsl(Or((a, b))) == {
        "bool": {"should": [compile_query_dsl(a), compile_query_dsl(b)], "minimum_should_match": 1}
    }


def test_no_filter_is_none() -> None:
    assert compile_filter(None) is None
    assert compile_filter(c("a", "$eq", 1)) == {"term": {"a": {"value": 1}}}


@pytest.mark.parametrize(
    "name", ["", "a b", "a;b", "a`b", 'a"b', "1a", "a-b", "a.b", "a*", "x" * 65, "é", "a\n", "a\nb"]
)
def test_hostile_field_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="invalid field name"):
        compile_query_dsl(c(name, "$eq", 1))


def test_allow_list_of_fields() -> None:
    assert compile_filter(c("lang", "$eq", "en"), allowed={"lang"}) is not None
    with pytest.raises(ValueError, match="unknown field"):
        compile_filter(c("year", "$eq", 1), allowed={"lang"})
    with pytest.raises(ValueError, match="unknown field"):
        compile_filter(And((c("lang", "$eq", "en"), c("order", "$eq", "x"))), allowed={"lang"})


def test_values_are_json_scalars_never_interpolated() -> None:
    hostile = 'x"}}, {"match_all": {}} //'
    assert compile_query_dsl(c("lang", "$eq", hostile)) == {"term": {"lang": {"value": hostile}}}
    with pytest.raises(ValueError, match="Unicode"):
        compile_query_dsl(c("lang", "$eq", "bad\ud800"))
    with pytest.raises(ValueError, match="64 bits"):
        compile_query_dsl(c("year", "$eq", 2**63))
    with pytest.raises(ValueError, match="finite"):
        compile_query_dsl(c("score", "$lt", math.inf))
    with pytest.raises(ValueError, match="numbers only"):
        compile_query_dsl(c("lang", "$gt", "a"))


def _eval(query: Mapping[str, Any], doc: Mapping[str, Any]) -> bool:
    """A reference interpreter for the DSL subset the compiler emits, with Elasticsearch semantics:
    a document without the field matches no term/terms/range clause (so ``must_not`` keeps it)."""
    ((kind, body),) = query.items()
    if kind == "bool":
        ok = all(_eval(q, doc) for q in body.get("filter", []))
        ok = ok and not any(_eval(q, doc) for q in body.get("must_not", []))
        if "should" in body:
            ok = ok and any(_eval(q, doc) for q in body["should"])
        return ok
    ((fname, arg),) = body.items()
    if fname not in doc:
        return False
    actual = doc[fname]

    def same(a: Any, b: Any) -> bool:
        return isinstance(a, bool) == isinstance(b, bool) and a == b

    if kind == "term":
        return same(actual, arg["value"])
    if kind == "terms":
        return any(same(actual, v) for v in arg)
    assert kind == "range"
    ops = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b}
    return all(ops[o](actual, v) for o, v in arg.items())


def test_missing_fields_follow_matches_for_every_operator() -> None:
    present = {"document_id": "d", "lang": "en", "year": 2020, "score": 0.5, "public": True}
    absent_cases = [present, {"document_id": "d"}, {"document_id": "d", "lang": "en"}, {"lang": "fr", "public": False}]
    schema = full_schema()
    conds: list[dict[str, Any]] = []
    for fname, ftype in schema.items():
        value = {"str": "en", "int": 2020, "float": 0.5, "bool": True}[ftype]
        for op in ("$eq", "$ne"):
            conds.append({fname: {op: value}})
        for op in ("$in", "$nin"):
            conds.append({fname: {op: [value]}})
        if ftype in ("int", "float"):
            conds += [{fname: {op: value}} for op in ("$gt", "$gte", "$lt", "$lte")]
    for raw in conds:
        f = parse_filter(raw, schema)
        assert f is not None
        for doc in absent_cases:
            assert _eval(compile_query_dsl(f), doc) == matches(f, doc), (raw, doc)


def test_random_filters_agree_with_the_reference_evaluator() -> None:
    rng = random.Random(2024)
    schema = full_schema()
    for _ in range(400):
        f = parse_filter(random_filter(rng), schema)
        assert f is not None
        doc = {k: v for k, v in random_metadata(rng).items() if rng.random() < 0.7}  # some fields absent
        assert _eval(compile_query_dsl(f), doc) == matches(f, doc), (f, doc)


# ---- no server needed: config, registration, module shadowing --------------------------------


def test_client_module_is_not_shadowed_by_the_store_module() -> None:
    if not HAVE_CLIENT:
        pytest.skip("the 'elasticsearch' extra is not installed")
    client = importlib.import_module("elasticsearch")
    assert hasattr(client, "Elasticsearch")
    assert "ragcentre" not in str(client.__file__)
    assert es_module.__name__ == "ragcentre.stores.elasticsearch"


def test_config_needs_exactly_one_target() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ElasticsearchConfig()
    with pytest.raises(ValidationError, match="exactly one"):
        ElasticsearchConfig(url="http://x:9200", cloud_id="a:b")
    assert ElasticsearchConfig(url="http://x:9200").index_options == "hnsw"
    assert ElasticsearchConfig(cloud_id="name:abc").url is None


@pytest.mark.parametrize("url", ["localhost:9200", "ftp://x", "http://", "x"])
def test_config_rejects_bad_urls(url: str) -> None:
    with pytest.raises(ValidationError, match="http"):
        ElasticsearchConfig(url=url)


@pytest.mark.parametrize("prefix", ["Upper_", "_x", "-x", ".x", "a b", "a/b", "a*", "a,b", "x" * 101])
def test_config_rejects_bad_prefixes(prefix: str) -> None:
    with pytest.raises(ValidationError):
        ElasticsearchConfig(url="http://x:9200", prefix=prefix)


def test_config_rejects_unknown_fields_and_bad_options() -> None:
    with pytest.raises(ValidationError):
        ElasticsearchConfig(url="http://x:9200", bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ElasticsearchConfig(url="http://x:9200", index_options="ivf")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ElasticsearchConfig(url="http://x:9200", num_candidates_factor=0)
    ElasticsearchConfig(url="http://x:9200", prefix="ragcentre_", index_options="int8_hnsw", num_candidates_factor=20)


def test_store_type_registration() -> None:
    assert STORE_TYPE.type == "elasticsearch"
    assert STORE_TYPE.immutable_fields == ("index_options",)
    assert set(STORE_TYPE.secret_keys) == {"api_key", "password"}
    assert STORE_TYPE.capabilities.max_dim == 4096
    assert STORE_TYPE.capabilities.metrics == {"cosine", "l2", "dot"}


@pytest.mark.skipif(HAVE_CLIENT, reason="only meaningful when the extra is absent")
def test_missing_extra_is_a_clear_error() -> None:
    with pytest.raises(StoreUnavailable, match="elasticsearch"):
        ElasticsearchStore.connect(url="http://x:9200")


@needs_client
def test_connect_validates_credentials() -> None:
    with pytest.raises(StoreError, match="either an api_key or a password"):
        ElasticsearchStore.connect(url="http://x:9200", api_key="k", password="p")
    with pytest.raises(StoreError, match="exactly one"):
        ElasticsearchStore.connect()


# ---- no server needed: the shared store logic against an in-memory engine --------------------


class FakeEngineStore(SearchEngineStore):
    """An in-memory index registry standing in for the engine, to test catalog and write logic."""

    label = "fake"
    capabilities = es_module.CAPABILITIES

    def __init__(self, *, options: dict[str, Any] | None = None, prefix: str = "") -> None:
        super().__init__(None, prefix=prefix, num_candidates_factor=10, options=options or {"index_options": "hnsw"})
        self.indices: dict[str, dict[str, Any]] = {}
        self.docs: dict[str, dict[str, dict[str, Any]]] = {}
        self.bulk_calls: list[list[dict[str, Any]]] = []
        self.reject: str | None = None

    def _info(self) -> dict[str, Any]:
        return {"version": {"number": "9.5.1"}}

    def _get_mappings(self, index: str) -> dict[str, Any] | None:
        return self.indices.get(index)

    def _create_index(self, index: str, mappings: dict[str, Any]) -> None:
        self.indices[index] = {"dynamic": mappings["dynamic"], "_meta": mappings["_meta"],
                               "properties": dict(mappings["properties"])}  # fmt: skip
        self.docs[index] = {}

    def _put_mappings(self, index: str, mappings: dict[str, Any]) -> None:
        self.indices[index]["properties"].update(mappings["properties"])
        self.indices[index]["_meta"] = mappings["_meta"]

    def _delete_index(self, index: str) -> None:
        self.indices.pop(index, None)
        self.docs.pop(index, None)

    def _bulk(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        self.bulk_calls.append(operations)
        items: list[dict[str, Any]] = []
        for action, doc in self._pairs(operations):
            ((kind, head),) = action.items()
            if head["_index"] not in self.docs:
                gone = StoreError("index_not_found_exception")
                gone.status_code = 404  # type: ignore[attr-defined]
                raise gone
            if self.reject:
                items.append({kind: {"status": 400, "error": {"type": "x", "reason": self.reject}}})
            elif kind == "index":
                self.docs[head["_index"]][head["_id"]] = doc or {}
                items.append({kind: {"status": 201}})
            else:
                found = self.docs[head["_index"]].pop(head["_id"], None) is not None
                items.append({kind: {"status": 200 if found else 404, "result": "deleted" if found else "not_found"}})
        return {"errors": any("error" in next(iter(i.values())) for i in items), "items": items}

    @staticmethod
    def _pairs(operations: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        out: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        i = 0
        while i < len(operations):
            if "delete" in operations[i]:
                out.append((operations[i], None))
                i += 1
            else:
                out.append((operations[i], operations[i + 1]))
                i += 2
        return out

    def _count(self, index: str) -> int:
        return len(self.docs[index])

    def _knn(self, index: str, entry: CatalogEntry, vector: Sequence[float], k: int, flt: dict[str, Any] | None) -> Any:
        raise NotImplementedError

    def _vector_mapping(self, dim: int, metric: Metric) -> dict[str, Any]:
        return {"type": "dense_vector", "dims": dim, "similarity": metric}

    def _check_server(self, entry_options: Mapping[str, Any], metric: Metric) -> None:
        return None


NAME = "rc_fake_0001"


def spec(dim: int = 4, metric: Metric = "cosine", fields: dict[str, Any] | None = None) -> CollectionSpec:
    return CollectionSpec(NAME, dim, metric, dict(SCHEMA_FIELDS) if fields is None else fields)


def test_catalog_round_trip_in_the_mapping_meta() -> None:
    store = FakeEngineStore(prefix="pre_")
    assert store.collection_info(NAME) is None
    store.ensure_collection(spec())
    mapping = store.indices["pre_" + NAME]
    assert mapping["dynamic"] == "strict"
    stored = CatalogEntry.from_json(__import__("json").dumps(mapping["_meta"]["ragcentre"]))
    assert (stored.dim, stored.metric) == (4, "cosine")
    assert stored.options == {"index_options": "hnsw"}
    assert stored.fields == {"document_id": "str", **SCHEMA_FIELDS}
    assert mapping["properties"]["year"] == {"type": "long"}
    assert mapping["properties"]["score"] == {"type": "double"}
    assert mapping["properties"]["lang"] == {"type": "keyword"}
    assert mapping["properties"]["public"] == {"type": "boolean"}
    assert mapping["properties"]["document_id"] == {"type": "keyword"}
    info = store.collection_info(NAME)
    assert info is not None and (info.dim, info.metric, info.count) == (4, "cosine", 0)
    store.ensure_collection(spec())  # idempotent


def test_catalog_mismatches() -> None:
    store = FakeEngineStore()
    store.ensure_collection(spec())
    with pytest.raises(CollectionMismatch):
        store.ensure_collection(spec(dim=5))
    with pytest.raises(CollectionMismatch):
        store.ensure_collection(spec(metric="l2"))
    with pytest.raises(CollectionMismatch):
        store.ensure_collection(spec(fields={"year": "str"}))
    other = FakeEngineStore(options={"index_options": "flat"})
    other.indices, other.docs = store.indices, store.docs
    with pytest.raises(CollectionMismatch, match="options"):
        other.ensure_collection(spec())


def test_adding_a_field_keeps_old_ones() -> None:
    store = FakeEngineStore()
    store.ensure_collection(spec(fields={"lang": "str"}))
    store.ensure_collection(spec(fields={"lang": "str", "year": "int"}))
    entry = store._catalog.get(NAME)
    assert entry is not None and entry.fields == {"document_id": "str", "lang": "str", "year": "int"}
    assert store.indices[NAME]["properties"]["year"] == {"type": "long"}


def test_index_not_created_by_ragcentre_is_refused() -> None:
    store = FakeEngineStore()
    store.indices[NAME] = {"properties": {"title": {"type": "text"}}}
    with pytest.raises(StoreError, match="not created by RAGCentre"):
        store.ensure_collection(spec())
    with pytest.raises(StoreError, match="not created by RAGCentre"):
        store.collection_info(NAME)


def test_reserved_and_invalid_field_names() -> None:
    store = FakeEngineStore()
    with pytest.raises(StoreError, match="reserved"):
        store.ensure_collection(spec(fields={"_rc_vector": "str"}))
    with pytest.raises(ValueError, match="invalid field name"):
        store.ensure_collection(spec(fields={"a b": "str"}))


def test_physical_names_use_the_prefix() -> None:
    store = FakeEngineStore(prefix="my-prefix.")
    store.ensure_collection(spec())
    assert list(store.indices) == ["my-prefix." + NAME]
    store.drop_collection(NAME)
    assert store.indices == {} and store.collection_info(NAME) is None
    store.drop_collection(NAME)  # missing: fine
    with pytest.raises(StoreError, match="invalid collection name"):
        store.ensure_collection(CollectionSpec("Bad Name", 4, "cosine"))


def test_max_dim_is_enforced_before_anything_is_created() -> None:
    store = FakeEngineStore()
    with pytest.raises(StoreError, match="4096"):
        store.ensure_collection(spec(dim=4097))
    assert store.indices == {}


def _rec(i: int, dim: int = 4, **meta: Any) -> Record:
    base = {"document_id": f"d{i}", "lang": "en", "year": 2020, "score": 1, "public": True}
    return Record(str(uuid.UUID(int=i + 1)), [1.0, float(i + 1), 0.5, 0.25][:dim], {**base, **meta, "stray": "x"})


def test_upsert_documents_carry_only_known_fields_and_float_fields_are_floats() -> None:
    store = FakeEngineStore()
    store.ensure_collection(spec())
    store.upsert(NAME, [_rec(0)])
    (doc,) = store.docs[NAME].values()
    assert set(doc) == {"_rc_vector", "document_id", "lang", "year", "score", "public"}
    assert isinstance(doc["score"], float) and doc["score"] == 1.0
    assert doc["_rc_vector"] == [1.0, 1.0, 0.5, 0.25]
    assert store.bulk_calls[0][0] == {"index": {"_index": NAME, "_id": str(uuid.UUID(int=1))}}


def test_upsert_validation() -> None:
    store = FakeEngineStore()
    with pytest.raises(CollectionNotFound):
        store.upsert(NAME, [_rec(0)])
    store.ensure_collection(spec())
    store.upsert(NAME, [])
    with pytest.raises(StoreError, match="dimension"):
        store.upsert(NAME, [Record(str(uuid.uuid4()), [1.0], {})])
    with pytest.raises(StoreError, match="non-finite"):
        store.upsert(NAME, [Record(str(uuid.uuid4()), [1.0, math.nan, 0.0, 0.0], {})])
    with pytest.raises(StoreError, match="zero vector"):
        store.upsert(NAME, [Record(str(uuid.uuid4()), [0.0] * 4, {})])
    with pytest.raises(StoreError, match="exceeds max_batch"):
        store.upsert(NAME, [_rec(i) for i in range(store.capabilities.max_batch + 1)])
    assert store.bulk_calls == []  # nothing reached the engine
    store.ensure_collection(CollectionSpec("rc_fake_0002", 4, "dot"))
    store.upsert("rc_fake_0002", [Record(str(uuid.uuid4()), [0.0] * 4, {})])  # a zero vector is fine for dot


def test_bulk_item_errors_surface() -> None:
    store = FakeEngineStore()
    store.ensure_collection(spec())
    store.reject = "failed to parse"
    with pytest.raises(StoreError, match=r"rejected.*failed to parse"):
        store.upsert(NAME, [_rec(0), _rec(1)])


def test_delete_ignores_missing_ids_and_collections() -> None:
    store = FakeEngineStore()
    store.delete(NAME, ["x"])  # no collection: nothing happens
    store.ensure_collection(spec())
    store.upsert(NAME, [_rec(0), _rec(1)])
    store.delete(NAME, [str(uuid.UUID(int=1)), str(uuid.uuid4())])
    store.delete(NAME, [])
    info = store.collection_info(NAME)
    assert info is not None and info.count == 1
    store.reject = "nope"
    with pytest.raises(StoreError, match="rejected"):
        store.delete(NAME, [str(uuid.UUID(int=2))])


def test_query_validation_and_filter_errors() -> None:
    store = FakeEngineStore()
    with pytest.raises(CollectionNotFound):
        store.query(NAME, [1.0] * 4, 3)
    store.ensure_collection(spec())
    assert store.query(NAME, [1.0] * 4, 0) == []
    with pytest.raises(StoreError, match="dimension"):
        store.query(NAME, [1.0], 3)
    with pytest.raises(StoreError, match="cannot compile"):
        store.query(NAME, [1.0] * 4, 3, Cond("nope", "$eq", 1))  # type: ignore[arg-type]


def test_num_candidates_is_at_least_k_and_capped() -> None:
    store = FakeEngineStore()
    assert store.num_candidates(1) == 10
    assert store.num_candidates(100) == 1000
    assert store.num_candidates(5000) == 10_000
    assert store.num_candidates(10_000) == 10_000


# ---- no server needed: the requests the Elasticsearch store sends ------------------------------


class _Resp:
    def __init__(self, body: Any) -> None:
        self.body = body


class RecordingIndices:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.existing: dict[str, Any] = {}

    def get_mapping(self, index: str) -> _Resp:
        if index not in self.existing:
            err = StoreError("missing")
            err.status_code = 404  # type: ignore[attr-defined]
            raise err
        return _Resp({index: {"mappings": self.existing[index]}})

    def create(self, index: str, mappings: dict[str, Any]) -> None:
        self.created.append({"index": index, "mappings": mappings})
        self.existing[index] = mappings

    def put_mapping(self, index: str, properties: dict[str, Any], meta: dict[str, Any]) -> None:
        self.existing[index]["properties"].update(properties)
        self.existing[index]["_meta"] = meta

    def delete(self, index: str, ignore_unavailable: bool) -> None:
        self.existing.pop(index, None)


class RecordingClient:
    def __init__(self, hits: list[dict[str, Any]] | None = None) -> None:
        self.indices = RecordingIndices()
        self.searches: list[dict[str, Any]] = []
        self.bulks: list[dict[str, Any]] = []
        self.hits = hits or []

    def info(self) -> _Resp:
        return _Resp({"version": {"number": "9.5.1"}})

    def bulk(self, operations: list[Any], refresh: str) -> _Resp:
        self.bulks.append({"operations": operations, "refresh": refresh})
        return _Resp({"errors": False, "items": []})

    def search(self, **kwargs: Any) -> _Resp:
        self.searches.append(kwargs)
        return _Resp({"hits": {"hits": self.hits}})

    def count(self, index: str) -> _Resp:
        return _Resp({"count": 3})

    def close(self) -> None:
        pass


def test_mapping_request_shape() -> None:
    client = RecordingClient()
    store = ElasticsearchStore(client, prefix="p_", index_options="int8_hnsw")
    store.ensure_collection(CollectionSpec(NAME, 16, "dot", {"year": "int"}))
    (created,) = client.indices.created
    assert created["index"] == "p_" + NAME
    vec = created["mappings"]["properties"]["_rc_vector"]
    assert vec == {
        "type": "dense_vector", "dims": 16, "index": True, "similarity": "max_inner_product",
        "index_options": {"type": "int8_hnsw"},
    }  # fmt: skip
    assert created["mappings"]["_meta"]["ragcentre"]["options"] == {"index_options": "int8_hnsw"}
    assert {m: store._vector_mapping(16, m)["similarity"] for m in ("cosine", "l2", "dot")} == {
        "cosine": "cosine", "l2": "l2_norm", "dot": "max_inner_product",
    }  # fmt: skip


def test_bbq_needs_64_dimensions() -> None:
    store = ElasticsearchStore(RecordingClient(), index_options="bbq_hnsw")
    with pytest.raises(StoreError, match="64 dimensions"):
        store.ensure_collection(CollectionSpec(NAME, 8, "cosine"))
    store.ensure_collection(CollectionSpec(NAME, 64, "cosine"))


def test_bulk_waits_for_refresh() -> None:
    client = RecordingClient()
    store = ElasticsearchStore(client)
    store.ensure_collection(CollectionSpec(NAME, 4, "cosine", {"lang": "str"}))
    store.upsert(NAME, [Record(str(uuid.uuid4()), [1.0, 0.0, 0.0, 0.0], {"lang": "en"})])
    store.delete(NAME, ["a"])
    assert [b["refresh"] for b in client.bulks] == ["wait_for", "wait_for"]


def test_query_puts_the_filter_inside_knn_and_inverts_scores() -> None:
    hits = [{"_id": "a", "_score": 0.9}, {"_id": "b", "_score": 0.75}]
    client = RecordingClient(hits)
    store = ElasticsearchStore(client, num_candidates_factor=7)
    store.ensure_collection(CollectionSpec(NAME, 4, "cosine", {"lang": "str"}))
    got = store.query(NAME, [1, 0, 0, 0], 5, Cond("lang", "$eq", "en"))
    (search,) = client.searches
    assert "query" not in search and "post_filter" not in search  # no sibling or post filter
    knn = search["knn"]
    assert knn["filter"] == {"term": {"lang": {"value": "en"}}}
    assert (knn["k"], knn["num_candidates"], knn["field"]) == (5, 35, "_rc_vector")
    assert "rescore_vector" not in knn  # full precision needs no rescoring
    assert search["size"] == 5 and search["source"] is False
    assert [(m.id, round(m.score, 6)) for m in got] == [("a", 0.8), ("b", 0.5)]


def test_quantised_queries_rescore() -> None:
    client = RecordingClient()
    store = ElasticsearchStore(client, index_options="int8_hnsw")
    store.ensure_collection(CollectionSpec(NAME, 4, "l2"))
    store.query(NAME, [1, 0, 0, 0], 3)
    assert client.searches[0]["knn"]["rescore_vector"] == {"oversample": 3.0}
    assert "filter" not in client.searches[0]["knn"]


# ---- live: behaviour the contract suite does not reach ----------------------------------------


class TestElasticsearchLive:
    NAME = "rc_live_0001"

    @pytest.fixture
    def store(self, prefix: str) -> Iterator[ElasticsearchStore]:
        s = ElasticsearchStore.connect(url=URL, prefix=prefix)
        yield s
        s.close()

    def test_writes_are_visible_at_once(self, store: ElasticsearchStore) -> None:
        store.ensure_collection(CollectionSpec(self.NAME, 4, "cosine", {"lang": "str"}))
        rid = str(uuid.uuid4())
        store.upsert(self.NAME, [Record(rid, [1.0, 0.0, 0.0, 0.0], {"document_id": "d", "lang": "en"})])
        info = store.collection_info(self.NAME)
        assert info is not None and info.count == 1  # no refresh in between: refresh=wait_for
        assert [m.id for m in store.query(self.NAME, [1.0, 0.0, 0.0, 0.0], 1)] == [rid]
        store.delete(self.NAME, [rid])
        info = store.collection_info(self.NAME)
        assert info is not None and info.count == 0
        assert store.capabilities.consistency == "immediate"

    def test_missing_field_semantics_on_the_server(self, store: ElasticsearchStore) -> None:
        """Documents written without a field satisfy only $ne and $nin, as ``matches`` says."""
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
        assert ids({"year": {"$eq": 2020}}) == {with_all}
        assert ids({"$or": [{"lang": "en"}, {"year": {"$gt": 2020}}]}) == {with_all, no_year, no_lang}
        assert ids({"$and": [{"lang": {"$ne": "fr"}}, {"year": {"$lt": 2021}}]}) == {with_all}

    def test_delete_on_a_missing_collection_is_quiet(self, store: ElasticsearchStore) -> None:
        store.delete("rc_gone_0001", [str(uuid.uuid4())])
        store.drop_collection("rc_gone_0001")

    def test_zero_vector_with_cosine_is_a_clear_error(self, store: ElasticsearchStore) -> None:
        store.ensure_collection(CollectionSpec(self.NAME, 4, "cosine"))
        with pytest.raises(StoreError, match="zero vector"):
            store.upsert(self.NAME, [Record(str(uuid.uuid4()), [0.0] * 4, {})])

    def test_server_rejects_dimensions_above_the_declared_limit(self, store: ElasticsearchStore) -> None:
        assert store.capabilities.max_dim == 4096
        store.ensure_collection(CollectionSpec(self.NAME, 4096, "cosine"))  # the largest the server accepts
        with pytest.raises(StoreError, match="4096"):
            store.ensure_collection(CollectionSpec("rc_live_0002", 4097, "cosine"))

    def test_foreign_index_is_not_adopted(self, store: ElasticsearchStore) -> None:
        store._client.indices.create(index=store._physical(self.NAME), mappings={"properties": {"t": {"type": "text"}}})
        with pytest.raises(StoreError, match="not created by RAGCentre"):
            store.ensure_collection(CollectionSpec(self.NAME, 4, "cosine"))

    def test_second_ensure_with_new_fields_adds_mappings(self, store: ElasticsearchStore) -> None:
        store.ensure_collection(CollectionSpec(self.NAME, 4, "cosine", {"lang": "str"}))
        store.ensure_collection(CollectionSpec(self.NAME, 4, "cosine", {"lang": "str", "year": "int"}))
        props = store._client.indices.get_mapping(index=store._physical(self.NAME)).body
        mapping = props[store._physical(self.NAME)]["mappings"]
        assert mapping["properties"]["year"] == {"type": "long"}
        assert mapping["dynamic"] == "strict"
        assert mapping["_meta"]["ragcentre"]["fields"] == {"document_id": "str", "lang": "str", "year": "int"}

    def test_concurrent_ensure_creates_one_collection(self, prefix: str) -> None:
        import threading

        stores = [ElasticsearchStore.connect(url=URL, prefix=prefix) for _ in range(6)]
        errors: list[BaseException] = []

        def go(s: ElasticsearchStore) -> None:
            try:
                s.ensure_collection(CollectionSpec(self.NAME, 4, "cosine", {"lang": "str"}))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=go, args=(s,)) for s in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors, errors
        info = stores[0].collection_info(self.NAME)
        assert info is not None and info.dim == 4
        for s in stores:
            s.close()
