"""Weaviate adapter: pure unit tests, and the contract suite against a real Weaviate server.

Server tests need ``docker run -p 58080:8080 -p 50051:50051 -e AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true
-e DEFAULT_VECTORIZER_MODULE=none cr.weaviate.io/semitechnologies/weaviate:1.36.2`` (the tests skip when
nothing answers). Override with ``RAGCENTRE_TEST_WEAVIATE_HOST`` / ``_PORT`` / ``_GRPC_PORT``. Every test
uses its own class-name prefix and removes the collections it created.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

pytest.importorskip("weaviate")

from pydantic import ValidationError

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import parse_filter
from ragcentre.stores import weaviate as wmod
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, Record, StoreError, VectorStore
from ragcentre.stores.weaviate import (
    DESC_PREFIX,
    STORE_TYPE,
    WeaviateConfig,
    WeaviateStore,
    logical_name,
    object_uuid,
    physical_name,
    property_name,
    score,
    translate_filter,
)
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract

HOST = os.environ.get("RAGCENTRE_TEST_WEAVIATE_HOST", "localhost")
PORT = int(os.environ.get("RAGCENTRE_TEST_WEAVIATE_PORT", "58080"))
GRPC_PORT = int(os.environ.get("RAGCENTRE_TEST_WEAVIATE_GRPC_PORT", "50051"))

SCHEMA: dict[str, Any] = {"lang": "str", "year": "int", "score": "float", "public": "bool", "document_id": "str"}


# ---- unit tests (no server) ----------------------------------------------------------------


def test_name_mapping_round_trip() -> None:
    assert physical_name("", "rc_abc_0123456789abcdef") == "Rc_abc_0123456789abcdef"
    assert physical_name("team_", "rc_abc") == "Team_rc_abc"
    for prefix in ("", "a", "team_", "ab_1"):
        for name in ("rc_x", "rc_0123_abcdef", "a_b"):
            assert logical_name(prefix, physical_name(prefix, name)) == name
    assert logical_name("team_", "Other_rc_x") is None
    assert logical_name("", "") is None


def test_name_mapping_is_injective_per_prefix() -> None:
    names = ["rc_a", "rc_b", "rc_a_", "rc_a1"]
    assert len({physical_name("p_", n) for n in names}) == len(names)


def test_physical_name_is_validated() -> None:
    with pytest.raises(StoreError):
        physical_name("", "Rc_Upper")  # not a valid logical name
    with pytest.raises(StoreError):
        physical_name("", "rc-x")
    with pytest.raises(StoreError):
        physical_name("x" * 250, "rc_abcdef")  # longer than Weaviate's 255 characters


def test_property_names_avoid_reserved_words_and_case_merging() -> None:
    for field in ("id", "_id", "_additional", "vector", "name", "document_id"):
        assert property_name(field) == "f_" + field
    assert property_name("Name") == "fu__name"
    names = [
        "Name",
        "name",
        "NAME",
        "nAME",
        "_name",
        "_Name",
        "__name",
        "a_B",
        "a_b",
        "aB",
        "A_b",
        "a__b",
        "fu_x",
        "f_x",
        "x",
    ]
    mapped = [property_name(n) for n in names]
    assert all(m == m.lower() and m[0].isalpha() for m in mapped)
    assert len({m.lower() for m in mapped}) == len(names)  # injective, even case-insensitively
    with pytest.raises(ValueError):
        property_name("a-b")


def test_object_uuid() -> None:
    canonical = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
    assert object_uuid(canonical) == uuid.UUID(canonical)
    assert str(object_uuid(canonical)) == canonical
    upper = canonical.upper()
    assert str(object_uuid(upper)) != canonical  # not the lower-case twin: ids are distinct strings
    assert object_uuid(upper) == object_uuid(upper)  # deterministic
    assert object_uuid("not a uuid") == object_uuid("not a uuid") != object_uuid("not a uuid ")
    assert object_uuid("{" + canonical + "}") != uuid.UUID(canonical)


def test_scores() -> None:
    assert score("cosine", 0.25) == pytest.approx(0.75)
    assert score("cosine", 1.0) == 0.0  # zero vector
    assert score("dot", -3.5) == pytest.approx(3.5)
    assert score("l2", 25.0) == pytest.approx(-5.0)  # l2-squared -> -Euclidean
    assert score("l2", -1e-9) == 0.0


def shape(x: Any) -> Any:
    if hasattr(x, "filters"):
        return (x.operator.name, [shape(i) for i in x.filters])
    return (x.operator.name, x.target, x.value)


def compile_(raw: Any) -> Any:
    flt = parse_filter(raw, SCHEMA)
    assert flt is not None
    return shape(translate_filter(flt, {k: v for k, v in SCHEMA.items() if k != "document_id"}))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"lang": "en"}, ("EQUAL", "f_lang", '"en"')),
        ({"lang": {"$ne": "en"}}, ("NOT_EQUAL", "f_lang", '"en"')),
        ({"year": {"$gt": 2018}}, ("GREATER_THAN", "f_year", 2018)),
        ({"year": {"$gte": 2018}}, ("GREATER_THAN_EQUAL", "f_year", 2018)),
        ({"year": {"$lt": 2018}}, ("LESS_THAN", "f_year", 2018)),
        ({"year": {"$lte": 2018}}, ("LESS_THAN_EQUAL", "f_year", 2018)),
        ({"score": {"$gte": 1}}, ("GREATER_THAN_EQUAL", "f_score", 1.0)),
        ({"score": 1}, ("EQUAL", "f_score", 1.0)),
        ({"public": True}, ("EQUAL", "f_public", True)),
        ({"public": {"$ne": False}}, ("NOT_EQUAL", "f_public", False)),
        ({"document_id": "d1"}, ("EQUAL", "f_document_id", '"d1"')),
        ({"lang": {"$in": ["en", "fr"]}}, ("CONTAINS_ANY", "f_lang", ['"en"', '"fr"'])),
        ({"year": {"$in": [1, 2]}}, ("CONTAINS_ANY", "f_year", [1, 2])),
        ({"score": {"$in": [1, 0.5]}}, ("CONTAINS_ANY", "f_score", [1.0, 0.5])),
        (
            {"lang": {"$nin": ["en", "fr"]}},
            ("AND", [("NOT_EQUAL", "f_lang", '"en"'), ("NOT_EQUAL", "f_lang", '"fr"')]),
        ),
        ({"lang": {"$nin": ["en"]}}, ("NOT_EQUAL", "f_lang", '"en"')),
    ],
)
def test_filter_operators(raw: Any, expected: Any) -> None:
    assert compile_(raw) == expected


def test_filter_combinators() -> None:
    assert compile_({"$and": [{"lang": "en"}, {"year": {"$gt": 1}}]}) == (
        "AND",
        [("EQUAL", "f_lang", '"en"'), ("GREATER_THAN", "f_year", 1)],
    )
    assert compile_({"$or": [{"lang": "en"}, {"$and": [{"year": 1}, {"public": True}]}]}) == (
        "OR",
        [("EQUAL", "f_lang", '"en"'), ("AND", [("EQUAL", "f_year", 1), ("EQUAL", "f_public", True)])],
    )


HOSTILE = [
    "",
    " ",
    " en",
    "en ",
    "'; DROP TABLE x; --",
    '"; {"$or": []} //',
    "a*b",
    "%_",
    "\\",
    "\x00",
    "line\nbreak",
    " x",
    "日本語",
    "x" * 5000,
]


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_strings_stay_data(value: str) -> None:
    assert compile_({"lang": value}) == ("EQUAL", "f_lang", f'"{value}"')
    assert compile_({"lang": {"$in": [value, "z"]}}) == ("CONTAINS_ANY", "f_lang", [f'"{value}"', '"z"'])
    assert compile_({"lang": {"$nin": [value, "z"]}}) == (
        "AND",
        [("NOT_EQUAL", "f_lang", f'"{value}"'), ("NOT_EQUAL", "f_lang", '"z"')],
    )


def test_unknown_filter_field_is_a_store_error() -> None:
    flt = parse_filter({"lang": "en"}, SCHEMA)
    assert flt is not None
    with pytest.raises(StoreError, match="unknown filter field 'lang'"):
        translate_filter(flt, {"year": "int"})


def test_config_validation() -> None:
    assert WeaviateConfig(url="http://localhost:8080").prefix == ""
    assert WeaviateConfig(host="db.internal", port=8081, grpc_port=50052, secure=True).host == "db.internal"
    for bad in (
        {},  # neither url nor host
        {"url": "http://h", "host": "h"},
        {"url": "http://h", "port": 8080},
        {"url": "ftp://h"},
        {"url": "localhost:8080"},
        {"host": "h", "prefix": "Team_"},
        {"host": "h", "prefix": "1x"},
        {"host": "h", "prefix": "a-b"},
        {"host": "h", "unknown": 1},
        {"host": "h", "grpc_port": 0},
        {"host": "h", "hnsw_max_connections": 1},
    ):
        with pytest.raises(ValidationError):
            WeaviateConfig.model_validate(bad)


def test_store_type() -> None:
    assert STORE_TYPE.type == "weaviate"
    assert STORE_TYPE.secret_keys == ("api_key",)
    assert STORE_TYPE.immutable_fields == ("hnsw_max_connections", "hnsw_ef_construction")
    caps = STORE_TYPE.capabilities
    assert caps.metrics == frozenset({"cosine", "l2", "dot"})
    assert (caps.max_dim, caps.consistency, caps.filter_exact) == (65535, "immediate", True)


def test_missing_extra_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from ragcentre.stores.base import StoreUnavailable

    def boom(*a: Any, **k: Any) -> Any:
        raise StoreUnavailable("The weaviate vector store needs the optional 'weaviate' extra")

    monkeypatch.setattr(wmod, "require_extra", boom)
    with pytest.raises(StoreUnavailable, match="weaviate"):
        WeaviateStore(object())


def test_unreachable_server_is_a_store_error() -> None:
    with pytest.raises(StoreError, match="not reachable"):
        WeaviateStore.connect(host="127.0.0.1", port=1, grpc_port=2, timeout=2)


# ---- server tests --------------------------------------------------------------------------


def _connect(**kw: Any) -> WeaviateStore:
    return WeaviateStore.connect(host=HOST, port=PORT, grpc_port=GRPC_PORT, **kw)


@pytest.fixture(scope="module")
def server() -> Iterator[None]:
    try:
        probe = _connect(timeout=5)
    except StoreError as exc:
        pytest.skip(f"no Weaviate server at {HOST}:{PORT} (gRPC {GRPC_PORT}): {str(exc).splitlines()[0][:120]}")
    try:
        probe.healthcheck()
    except StoreError as exc:
        probe.close()
        pytest.skip(f"Weaviate at {HOST}:{PORT} is not usable: {str(exc).splitlines()[0][:120]}")
    probe.close()
    yield


def _new_prefix() -> str:
    return f"t{uuid.uuid4().hex[:10]}_"


def _drop_prefixed(prefix: str) -> None:
    store = _connect()
    try:
        for name in store._client.collections.list_all(simple=True):
            if name.startswith(prefix.capitalize()):
                store._client.collections.delete(name)
    finally:
        store.close()


@pytest.fixture
def prefix(server: None) -> Iterator[str]:
    p = _new_prefix()
    yield p
    _drop_prefixed(p)


@pytest.mark.integration
class TestWeaviateContract(VectorStoreContract):
    @pytest.fixture
    def store(self, prefix: str) -> Iterator[WeaviateStore]:
        s = _connect(prefix=prefix, hnsw_max_connections=24, hnsw_ef_construction=150, hnsw_ef=64)
        yield s
        s.close()


@pytest.mark.integration
class TestWeaviateOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"hnsw_max_connections": 16}
    options_b: ClassVar[dict[str, Any]] = {"hnsw_max_connections": 32}

    @pytest.fixture(autouse=True)
    def _server_prefix(self, prefix: str) -> None:
        self._pfx = prefix

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> WeaviateStore:
        return _connect(prefix=self._pfx, **options)


@pytest.mark.integration
def test_is_a_vector_store(prefix: str) -> None:
    s = _connect(prefix=prefix)
    try:
        assert isinstance(s, VectorStore)
        s.healthcheck()
    finally:
        s.close()


@pytest.mark.integration
def test_catalog_lives_in_the_class_description(prefix: str) -> None:
    s = _connect(prefix=prefix, hnsw_ef_construction=120)
    try:
        s.ensure_collection(CollectionSpec("rc_cat_0001", 3, "l2", {"lang": "str"}))
        cfg = s._client.collections.get(physical_name(prefix, "rc_cat_0001")).config.get()
        assert cfg.description.startswith(DESC_PREFIX)
        entry = json.loads(cfg.description[len(DESC_PREFIX) :])
        assert (entry["dim"], entry["metric"]) == (3, "l2")
        assert entry["fields"] == {"document_id": "str", "lang": "str"}
        assert entry["options"] == {"hnsw_ef_construction": 120, "hnsw_max_connections": None}
        assert "api_key" not in cfg.description
        info = s.collection_info("rc_cat_0001")
        assert info is not None and (info.dim, info.metric, info.count) == (3, "l2", 0)
        # a second store (another process, say) sees the same catalog and accepts the same spec
        other = _connect(prefix=prefix, hnsw_ef_construction=120)
        try:
            other.ensure_collection(CollectionSpec("rc_cat_0001", 3, "l2", {"lang": "str"}))
            with pytest.raises(CollectionMismatch):
                other.ensure_collection(CollectionSpec("rc_cat_0001", 4, "l2"))
            with pytest.raises(CollectionMismatch):
                other.ensure_collection(CollectionSpec("rc_cat_0001", 3, "l2", {"lang": "int"}))
        finally:
            other.close()
    finally:
        s.drop_collection("rc_cat_0001")
        s.close()


@pytest.mark.integration
def test_adding_fields_updates_properties_and_description(prefix: str) -> None:
    s = _connect(prefix=prefix)
    try:
        v = [1.0, 0.0]
        s.ensure_collection(CollectionSpec("rc_add_0001", 2, "cosine", {"lang": "str"}))
        s.upsert("rc_add_0001", [Record("6f9619ff-8b86-d011-b42d-00c04fc964ff", v, {"document_id": "d", "lang": "en"})])
        s.ensure_collection(CollectionSpec("rc_add_0001", 2, "cosine", {"lang": "str", "year": "int", "ok": "bool"}))
        s.ensure_collection(CollectionSpec("rc_add_0001", 2, "cosine", {"lang": "str", "year": "int", "ok": "bool"}))
        col = s._client.collections.get(physical_name(prefix, "rc_add_0001"))
        assert {"f_lang", "f_year", "f_ok", "f_document_id", "rc_id"} <= {p.name for p in col.config.get().properties}
        rec = Record(
            "8f9619ff-8b86-d011-b42d-00c04fc964ff",
            [0.0, 1.0],
            {"document_id": "d", "lang": "fr", "year": 2024, "ok": True},
        )
        s.upsert("rc_add_0001", [rec])
        flt = parse_filter({"year": {"$gte": 2024}}, {"year": "int", "document_id": "str"})
        assert [m.id for m in s.query("rc_add_0001", v, 5, flt)] == [rec.id]
        # metadata the collection does not declare is not filterable, so it is ignored
        s.upsert("rc_add_0001", [Record("9f9619ff-8b86-d011-b42d-00c04fc964ff", v, {"document_id": "d", "nope": 1})])
        assert col.aggregate.over_all(total_count=True).total_count == 3
    finally:
        s.drop_collection("rc_add_0001")
        s.close()


@pytest.mark.integration
def test_ids_round_trip_exactly(prefix: str) -> None:
    s = _connect(prefix=prefix)
    ids = [
        "6f9619ff-8b86-d011-b42d-00c04fc964ff",
        "6F9619FF-8B86-D011-B42D-00C04FC964FF",  # same UUID, other spelling: a distinct id here
        "{6f9619ff-8b86-d011-b42d-00c04fc964ff}",
        "not-a-uuid",
        "x" * 200,
    ]
    try:
        s.ensure_collection(CollectionSpec("rc_ids_0001", 2, "cosine"))
        s.upsert("rc_ids_0001", [Record(i, [1.0, float(n)], {"document_id": "d"}) for n, i in enumerate(ids)])
        got = {m.id for m in s.query("rc_ids_0001", [1.0, 1.0], 10)}
        assert got == set(ids)
        s.delete("rc_ids_0001", [ids[1], "never-existed"])
        assert {m.id for m in s.query("rc_ids_0001", [1.0, 1.0], 10)} == set(ids) - {ids[1]}
    finally:
        s.drop_collection("rc_ids_0001")
        s.close()


@pytest.mark.integration
def test_reserved_and_colliding_field_names(prefix: str) -> None:
    fields = {"id": "str", "vector": "int", "_additional": "bool", "Name": "str", "name": "str", "_id": "float"}
    s = _connect(prefix=prefix)
    try:
        s.ensure_collection(CollectionSpec("rc_names_0001", 2, "cosine", fields))  # type: ignore[arg-type]
        m1 = {"document_id": "d", "id": "a", "vector": 1, "_additional": True, "Name": "X", "name": "x", "_id": 0.5}
        m2 = {"document_id": "d", "id": "b", "vector": 2, "_additional": False, "Name": "x", "name": "X", "_id": 1.5}
        recs = [
            Record("6f9619ff-8b86-d011-b42d-00c04fc964ff", [1.0, 0.0], m1),
            Record("7f9619ff-8b86-d011-b42d-00c04fc964ff", [0.9, 0.1], m2),
        ]
        s.upsert("rc_names_0001", recs)
        schema = {**fields, "document_id": "str"}
        for raw, want in [
            ({"Name": "X"}, [recs[0].id]),
            ({"name": "X"}, [recs[1].id]),
            ({"id": "b"}, [recs[1].id]),
            ({"vector": {"$gt": 1}}, [recs[1].id]),
            ({"_additional": True}, [recs[0].id]),
            ({"_id": {"$lt": 1.0}}, [recs[0].id]),
        ]:
            flt = parse_filter(raw, schema)  # type: ignore[arg-type]
            assert [m.id for m in s.query("rc_names_0001", [1.0, 0.0], 5, flt)] == want, raw
    finally:
        s.drop_collection("rc_names_0001")
        s.close()


@pytest.mark.integration
def test_hostile_strings_match_exactly(prefix: str) -> None:
    """Edge whitespace, empty strings and wildcard characters must be compared as plain data."""
    values = ["en", " en", "en ", " en", "EN", "", " ", "a*b", "a%b", "'; --", '"', "\\", "en-US", "é", "é"]
    s = _connect(prefix=prefix)
    try:
        s.ensure_collection(CollectionSpec("rc_host_0001", 2, "cosine", {"lang": "str"}))
        recs = [
            Record(str(uuid.UUID(int=n + 1)), [1.0, 0.01 * n], {"document_id": "d", "lang": v})
            for n, v in enumerate(values)
        ]
        s.upsert("rc_host_0001", recs)
        by_value = {v: r.id for v, r in zip(values, recs, strict=True)}
        schema = {"lang": "str", "document_id": "str"}
        for v in values:
            flt = parse_filter({"lang": v}, schema)  # type: ignore[arg-type]
            assert [m.id for m in s.query("rc_host_0001", [1.0, 0.0], 50, flt)] == [by_value[v]], repr(v)
            flt = parse_filter({"lang": {"$ne": v}}, schema)  # type: ignore[arg-type]
            assert {m.id for m in s.query("rc_host_0001", [1.0, 0.0], 50, flt)} == set(by_value.values()) - {
                by_value[v]
            }
        flt = parse_filter({"lang": {"$in": ["en", "", "a*b"]}}, schema)  # type: ignore[arg-type]
        assert {m.id for m in s.query("rc_host_0001", [1.0, 0.0], 50, flt)} == {by_value[v] for v in ("en", "", "a*b")}
        flt = parse_filter({"lang": {"$nin": ["en", "", "a*b"]}}, schema)  # type: ignore[arg-type]
        assert {m.id for m in s.query("rc_host_0001", [1.0, 0.0], 50, flt)} == {
            by_value[v] for v in values if v not in ("en", "", "a*b")
        }
    finally:
        s.drop_collection("rc_host_0001")
        s.close()


@pytest.mark.integration
def test_missing_collection(prefix: str) -> None:
    s = _connect(prefix=prefix)
    try:
        with pytest.raises(CollectionNotFound):
            s.query("rc_missing_0001", [1.0, 0.0], 3)
        with pytest.raises(CollectionNotFound):
            s.upsert("rc_missing_0001", [Record(str(uuid.uuid4()), [1.0, 0.0], {})])
        s.delete("rc_missing_0001", [str(uuid.uuid4())])
        s.drop_collection("rc_missing_0001")
        assert s.collection_info("rc_missing_0001") is None
    finally:
        s.close()


@pytest.mark.integration
def test_drop_and_recreate_invalidates_cached_catalog(prefix: str) -> None:
    s = _connect(prefix=prefix)
    try:
        s.ensure_collection(CollectionSpec("rc_cache_0001", 2, "l2"))
        s.upsert("rc_cache_0001", [Record(str(uuid.uuid4()), [3.0, 4.0], {})])
        assert s.query("rc_cache_0001", [0.0, 0.0], 1)[0].score == pytest.approx(-5.0)
        s.drop_collection("rc_cache_0001")
        s.ensure_collection(CollectionSpec("rc_cache_0001", 3, "dot"))
        s.upsert("rc_cache_0001", [Record(str(uuid.uuid4()), [1.0, 2.0, 3.0], {})])
        assert s.query("rc_cache_0001", [1.0, 1.0, 1.0], 1)[0].score == pytest.approx(6.0)
    finally:
        s.drop_collection("rc_cache_0001")
        s.close()


@pytest.mark.integration
def test_foreign_collection_is_refused(prefix: str) -> None:
    s = _connect(prefix=prefix)
    try:
        s._client.collections.create(physical_name(prefix, "rc_foreign_0001"), description="someone else's")
        with pytest.raises(StoreError, match="not created by RAGCentre"):
            s.ensure_collection(CollectionSpec("rc_foreign_0001", 2, "cosine"))
        with pytest.raises(StoreError, match="not created by RAGCentre"):
            s.collection_info("rc_foreign_0001")
    finally:
        s.drop_collection("rc_foreign_0001")
        s.close()


@pytest.mark.integration
def test_query_limits_and_dimension_checks(prefix: str) -> None:
    s = _connect(prefix=prefix)
    try:
        s.ensure_collection(CollectionSpec("rc_lim_0001", 2, "cosine"))
        with pytest.raises(StoreError, match="at most 10000"):
            s.query("rc_lim_0001", [1.0, 0.0], 10001)
        assert s.query("rc_lim_0001", [1.0, 0.0], 0) == []
        with pytest.raises(StoreError, match="dimension"):
            s.query("rc_lim_0001", [1.0, 0.0, 0.0], 3)
        with pytest.raises(StoreError, match="dimension"):
            s.upsert("rc_lim_0001", [Record(str(uuid.uuid4()), [1.0], {})])
        with pytest.raises(StoreError, match="max_batch"):
            s.upsert("rc_lim_0001", [Record(str(uuid.uuid4()), [1.0, 0.0], {})] * 257)
    finally:
        s.drop_collection("rc_lim_0001")
        s.close()


@pytest.mark.integration
def test_hnsw_options_are_applied(prefix: str) -> None:
    s = _connect(prefix=prefix, hnsw_max_connections=20, hnsw_ef_construction=90, hnsw_ef=55)
    try:
        s.ensure_collection(CollectionSpec("rc_hnsw_0001", 2, "dot"))
        cfg = s._client.collections.get(physical_name(prefix, "rc_hnsw_0001")).config.get()
        index = cfg.vector_config["default"].vector_index_config
        assert (index.max_connections, index.ef_construction, index.ef) == (20, 90, 55)
        assert index.distance_metric.name == "DOT"
        # ef is a query-time option: another store with a different ef updates it, the build options stay
        other = _connect(prefix=prefix, hnsw_max_connections=20, hnsw_ef_construction=90, hnsw_ef=77)
        try:
            other.ensure_collection(CollectionSpec("rc_hnsw_0001", 2, "dot"))
        finally:
            other.close()
        cfg = s._client.collections.get(physical_name(prefix, "rc_hnsw_0001")).config.get()
        assert cfg.vector_config["default"].vector_index_config.ef == 77
    finally:
        s.drop_collection("rc_hnsw_0001")
        s.close()


@pytest.mark.integration
def test_factory_builds_a_working_store(prefix: str, tmp_path: Path) -> None:
    cfg = WeaviateConfig(url=f"http://{HOST}:{PORT}", grpc_port=GRPC_PORT, prefix=prefix)
    s = STORE_TYPE.factory(cfg, {}, tmp_path)
    try:
        s.healthcheck()
        s.ensure_collection(CollectionSpec("rc_fact_0001", 2, "cosine"))
        assert s.collection_info("rc_fact_0001") is not None
    finally:
        s.drop_collection("rc_fact_0001")
        s.close()


@pytest.mark.integration
def test_concurrent_ensure_from_separate_stores(prefix: str) -> None:
    """Workers (separate clients, as in separate processes) race to create the collection and add fields."""
    import threading

    errors: list[BaseException] = []
    fields_by_worker = [{"lang": "str"}, {"lang": "str", "year": "int"}, {"year": "int", "ok": "bool"}, {}] * 2

    def work(fields: dict[str, Any]) -> None:
        s = _connect(prefix=prefix)
        try:
            s.ensure_collection(CollectionSpec("rc_race_0001", 3, "cosine", fields))
        except BaseException as exc:
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=work, args=(f,)) for f in fields_by_worker]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    s = _connect(prefix=prefix)
    try:
        s.ensure_collection(CollectionSpec("rc_race_0001", 3, "cosine", {"lang": "str", "year": "int", "ok": "bool"}))
        cfg = s._client.collections.get(physical_name(prefix, "rc_race_0001")).config.get()
        assert {"f_lang", "f_year", "f_ok", "f_document_id"} <= {p.name for p in cfg.properties}
        entry = json.loads(cfg.description[len(DESC_PREFIX) :])
        assert entry["fields"] == {"document_id": "str", "lang": "str", "year": "int", "ok": "bool"}
    finally:
        s.drop_collection("rc_race_0001")
        s.close()


@pytest.mark.integration
def test_query_tuning_preserves_concurrent_field_addition(prefix: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store = _connect(prefix=prefix, hnsw_ef=77)
    name = "rc_tuning_race_0001"
    try:
        store.ensure_collection(CollectionSpec(name, 3, "cosine", {"group": "str"}))
        store.upsert(name, [Record("one", [1.0, 0.0, 0.0], {"group": "kept"})])
        physical = physical_name(prefix, name)
        rc = store._w.classes.config.Reconfigure
        store._client.collections.get(physical).config.update(
            vector_config=rc.Vectors.update(name="default", vector_index_config=rc.VectorIndex.hnsw(ef=32))
        )
        paused, writer_started, writer_done = Event(), Event(), Event()
        connection = store._client._connection
        original_put = connection.put

        def put(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("path") == f"/schema/{physical}" and not paused.is_set():
                paused.set()
                assert writer_started.wait(10)
                writer_done.wait(1)
            return original_put(*args, **kwargs)

        def add_field() -> None:
            writer_started.set()
            try:
                store.ensure_collection(CollectionSpec(name, 3, "cosine", {"year": "int"}))
            finally:
                writer_done.set()

        monkeypatch.setattr(connection, "put", put)
        with ThreadPoolExecutor(max_workers=2) as executor:
            query = executor.submit(store.query, name, [1.0, 0.0, 0.0], 1)
            assert paused.wait(10)
            writer = executor.submit(add_field)
            assert [match.id for match in query.result(timeout=20)] == ["one"]
            writer.result(timeout=20)
        cfg = store._client.collections.get(physical).config.get()
        entry = json.loads(cfg.description[len(DESC_PREFIX) :])
        assert entry["fields"] == {"document_id": "str", "group": "str", "year": "int"}
        assert {"f_document_id", "f_group", "f_year"} <= {prop.name for prop in cfg.properties}
        assert cfg.vector_config["default"].vector_index_config.ef == 77
    finally:
        store.drop_collection(name)
        store.close()
