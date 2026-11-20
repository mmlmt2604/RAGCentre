"""MongoDB vector store tests.

Pure unit tests (config, filter compiler, score conversion, index-state logic) need nothing. The
fake-client tests run the adapter and the whole contract suite against an in-memory stand-in for the
parts of pymongo the adapter uses (they need only the installed ``pymongo`` package). The live tests
need a MongoDB with search indexes, for example the Atlas Local image (see integration/t-mongodb.md):

    docker compose -p rc-t-mongodb -f <compose file> up -d
    RAGCENTRE_TEST_MONGODB_URI='mongodb://127.0.0.1:57017/?directConnection=true' \
        uv run pytest tests/test_vs_mongodb.py

They skip when the ``mongodb`` extra is missing or the server is unreachable or has no search support.
Each live test uses its own database and drops it afterwards. An Atlas cluster works too: set the URI
(never commit it); Atlas builds indexes more slowly, so allow minutes.
"""

from __future__ import annotations

import math
import os
import random
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import And, Cond, FieldType, matches, parse_filter
from ragcentre.stores import mongodb as mod
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, Record, StoreError, StoreUnavailable, VectorStore
from ragcentre.stores.mongodb import (
    CATALOG_COLLECTION,
    INDEX_NAME,
    STORE_TYPE,
    VECTOR_FIELD,
    MongoDBConfig,
    MongoDBStore,
    compile_filter,
    index_definition,
    index_state,
    parse_index_definition,
    to_score,
)
from ragcentre.testing.random_data import SCHEMA_FIELDS, full_schema, random_vector
from ragcentre.testing.vector_store_contract import DIM, VectorStoreContract, brute_score, make_records, wait_until

URI = os.environ.get("RAGCENTRE_TEST_MONGODB_URI", "mongodb://127.0.0.1:57017/?directConnection=true")
FIELDS: dict[str, FieldType] = {**SCHEMA_FIELDS, "document_id": "str"}


def _schema(raw: Any) -> Any:
    return parse_filter(raw, FIELDS)


def _compile(raw: Any) -> Any:
    f = _schema(raw)
    assert f is not None
    return compile_filter(f, FIELDS)


# ---- configuration --------------------------------------------------------------------------


def test_config_defaults_and_validation() -> None:
    cfg = MongoDBConfig()
    assert (cfg.database, cfg.exact, cfg.num_candidates_factor, cfg.prefix) == ("ragcentre", False, 20, "")
    assert MongoDBConfig(database="a-b_1", prefix="rc_", exact=True, num_candidates_factor=5).exact
    for bad in (
        {"database": ""},
        {"database": "has space"},
        {"database": "a.b"},
        {"database": "x" * 39},
        {"prefix": "a.b"},
        {"prefix": "$x"},
        {"prefix": "p" * 33},
        {"num_candidates_factor": 0},
        {"num_candidates_factor": 1001},
        {"unknown": 1},
        {"uri": "mongodb://secret"},  # the connection string is a secret, never configuration
    ):
        with pytest.raises(ValidationError):
            MongoDBConfig(**bad)


def test_store_type_shape() -> None:
    assert STORE_TYPE.type == "mongodb"
    assert STORE_TYPE.secret_keys == ("uri",)
    assert STORE_TYPE.immutable_fields == ()  # no index-shaping option: exact and the factor are query time
    caps = STORE_TYPE.capabilities
    assert caps.metrics == {"cosine", "l2"}  # dotProduct clamps at 0 and is not offered
    assert caps.consistency == "eventual" and caps.max_dim == 8192 and not caps.filter_exact


def test_capabilities_follow_exact(fake: Any) -> None:
    assert not MongoDBStore(fake, exact=False).capabilities.filter_exact
    assert MongoDBStore(fake, exact=True).capabilities.filter_exact
    assert MongoDBStore(fake).capabilities.consistency == "eventual"


def test_pymongo_is_not_shadowed_by_the_adapter_module() -> None:
    pymongo = pytest.importorskip("pymongo")
    assert mod._import() is pymongo
    assert hasattr(pymongo, "MongoClient")
    assert "stores" not in str(pymongo.__file__)


def test_missing_extra_gives_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__

    def deny(name: str, *a: Any, **kw: Any) -> Any:
        if name == "pymongo" or name.startswith("pymongo."):
            raise ImportError("no pymongo")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", deny)
    monkeypatch.delitem(__import__("sys").modules, "pymongo", raising=False)
    with pytest.raises(StoreUnavailable, match=r"ragcentre\[mongodb\]"):
        MongoDBStore.connect(uri="mongodb://x")


def test_factory_requires_the_uri_secret_and_checks_its_scheme(tmp_path: Path) -> None:
    pytest.importorskip("pymongo")
    with pytest.raises(StoreError, match="'uri' secret"):
        STORE_TYPE.factory(MongoDBConfig(), {}, tmp_path)
    with pytest.raises(StoreError) as bad:
        STORE_TYPE.factory(MongoDBConfig(), {"uri": "http://user:pw-123@host"}, tmp_path)
    assert "pw-123" not in str(bad.value)
    store = STORE_TYPE.factory(
        MongoDBConfig(database="d1", exact=True), {"uri": "mongodb://u:pw@127.0.0.1:1/"}, tmp_path
    )
    try:
        assert isinstance(store, VectorStore)  # no server is contacted until first use
        assert store.capabilities.filter_exact
    finally:
        store.close()


def test_connection_failures_never_echo_the_uri() -> None:
    pymongo = pytest.importorskip("pymongo")
    uri = "mongodb://someuser:s3cr3t-pw@127.0.0.1:1/?serverSelectionTimeoutMS=300&directConnection=true"
    store = MongoDBStore(pymongo.MongoClient(uri), scrub=uri)
    try:
        with pytest.raises(StoreError) as err:
            store.healthcheck()
    finally:
        store.close()
    assert "s3cr3t-pw" not in str(err.value) and "someuser" not in str(err.value)
    assert "mongodb:" in str(err.value)


# ---- the filter compiler --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"lang": "en"}, {"lang": {"$eq": "en"}}),
        ({"lang": {"$eq": "en"}}, {"lang": {"$eq": "en"}}),
        ({"lang": {"$ne": "en"}}, {"lang": {"$ne": "en"}}),
        ({"year": {"$gt": 2020}}, {"year": {"$gt": 2020}}),
        ({"year": {"$gte": 2020}}, {"year": {"$gte": 2020}}),
        ({"year": {"$lt": 2020}}, {"year": {"$lt": 2020}}),
        ({"year": {"$lte": 2020}}, {"year": {"$lte": 2020}}),
        ({"score": {"$gt": 0.5}}, {"score": {"$gt": 0.5}}),
        ({"score": 1}, {"score": {"$eq": 1}}),
        ({"public": True}, {"public": {"$eq": True}}),
        ({"public": {"$ne": False}}, {"public": {"$ne": False}}),
        ({"lang": {"$in": ["en", "de"]}}, {"lang": {"$in": ["en", "de"]}}),
        ({"lang": {"$nin": ["en", "de"]}}, {"lang": {"$nin": ["en", "de"]}}),
        ({"year": {"$in": [1, 2]}}, {"year": {"$in": [1, 2]}}),
        ({"document_id": "doc-01"}, {"document_id": {"$eq": "doc-01"}}),
    ],
)
def test_filter_compiler_every_operator(raw: Any, expected: Any) -> None:
    assert _compile(raw) == expected


def test_filter_compiler_combinators() -> None:
    assert _compile({"$and": [{"lang": "en"}, {"year": {"$gte": 2020}}]}) == {
        "$and": [{"lang": {"$eq": "en"}}, {"year": {"$gte": 2020}}]
    }
    assert _compile({"$or": [{"lang": "en"}, {"$and": [{"public": True}, {"year": {"$lt": 2018}}]}]}) == {
        "$or": [{"lang": {"$eq": "en"}}, {"$and": [{"public": {"$eq": True}}, {"year": {"$lt": 2018}}]}]
    }
    assert _compile({"lang": "en", "public": True}) == {
        "$and": [{"lang": {"$eq": "en"}}, {"public": {"$eq": True}}]
    }  # implicit and


def test_filter_compiler_keeps_values_as_values() -> None:
    # A hostile string is only ever a value: it never becomes an operator, a field or a path.
    nasty = ['{"$ne": null}', "$where", "a.b", "', $or: [{}]", "\x00", "$gt"]
    for value in nasty:
        assert _compile({"lang": value}) == {"lang": {"$eq": value}}
        assert _compile({"lang": {"$in": [value]}}) == {"lang": {"$in": [value]}}


@pytest.mark.parametrize("name", ["$where", "a.b", "$gt", "a b", "", "x" * 65, "1a", "é"])
def test_filter_compiler_rejects_bad_field_names(name: str) -> None:
    fields: dict[str, FieldType] = {name: "str"}
    with pytest.raises(StoreError, match="invalid filter field name"):
        compile_filter(Cond(name, "$eq", "x"), fields)


def test_filter_compiler_rejects_undeclared_fields_and_bad_values() -> None:
    with pytest.raises(StoreError, match="not a filterable field"):
        compile_filter(Cond("nope", "$eq", "x"), FIELDS)
    with pytest.raises(StoreError, match="expected a int"):
        compile_filter(Cond("year", "$eq", "x"), FIELDS)
    with pytest.raises(StoreError, match="finite"):
        compile_filter(Cond("score", "$gt", math.nan), FIELDS)
    with pytest.raises(StoreError, match="2\\*\\*53"):
        compile_filter(Cond("year", "$eq", 2**70), FIELDS)
    with pytest.raises(StoreError, match="does not apply"):
        compile_filter(Cond("lang", "$gt", "a"), FIELDS)
    with pytest.raises(StoreError, match="takes a list"):
        compile_filter(Cond("year", "$in", 3), FIELDS)
    with pytest.raises(StoreError, match="single value"):
        compile_filter(Cond("year", "$eq", (1, 2)), FIELDS)
    with pytest.raises(StoreError, match="unsupported"):
        compile_filter(Cond("year", "$regex", 1), FIELDS)  # type: ignore[arg-type]


def _mql_matches(expr: dict[str, Any], doc: dict[str, Any]) -> bool:
    """MQL semantics as verified on the server (see the live test of the same name): a missing field
    satisfies only $ne and $nin; booleans and numbers never equal each other."""
    for key, body in expr.items():
        if key == "$and":
            return all(_mql_matches(e, doc) for e in body)
        if key == "$or":
            return any(_mql_matches(e, doc) for e in body)
        (op, value), *_ = body.items()
        actual = doc.get(key)

        def eq(a: Any, b: Any) -> bool:
            if a is None:
                return False
            if isinstance(a, bool) or isinstance(b, bool):
                return isinstance(a, bool) and isinstance(b, bool) and a == b
            return bool(a == b)

        if op == "$eq":
            return eq(actual, value)
        if op == "$ne":
            return not eq(actual, value)
        if op == "$in":
            return any(eq(actual, v) for v in value)
        if op == "$nin":
            return not any(eq(actual, v) for v in value)
        if actual is None or isinstance(actual, (bool, str)):
            return False
        return {"$gt": actual > value, "$gte": actual >= value, "$lt": actual < value, "$lte": actual <= value}[op]
    raise AssertionError("empty expression")


def test_compiled_filters_agree_with_the_reference_evaluator_including_missing_fields() -> None:
    from ragcentre.testing.random_data import random_filter, random_metadata

    rng = random.Random(7)
    docs = [random_metadata(rng) for _ in range(300)]
    for d in docs[::3]:  # a third of the documents lack some fields (a field added later)
        for field in rng.sample(sorted(SCHEMA_FIELDS), rng.randint(1, 3)):
            d.pop(field)
    docs.append({})  # a document with no metadata at all
    for _ in range(200):
        f = parse_filter(random_filter(rng), full_schema())
        assert f is not None
        expr = compile_filter(f, FIELDS)
        for d in docs:
            assert _mql_matches(expr, d) == matches(f, d), (expr, d)


def test_ne_and_nin_are_plain_mql_operators_so_missing_fields_match() -> None:
    assert _compile({"lang": {"$ne": "en"}}) == {"lang": {"$ne": "en"}}  # no $exists guard that would hide them
    assert _compile({"lang": {"$nin": ["en"]}}) == {"lang": {"$nin": ["en"]}}
    assert matches(parse_filter({"lang": {"$ne": "en"}}, FIELDS), {}) is True


# ---- scores ---------------------------------------------------------------------------------


def test_score_conversion() -> None:
    assert to_score("cosine", 1.0) == pytest.approx(1.0)
    assert to_score("cosine", 0.5) == pytest.approx(0.0)
    assert to_score("cosine", 0.0) == pytest.approx(-1.0)
    assert to_score("cosine", 1.0 + 1e-6) == 1.0  # float32 overshoot is clamped
    assert to_score("l2", 1.0) == pytest.approx(0.0)
    assert to_score("l2", 0.5) == pytest.approx(-1.0)  # d**2 = 1/0.5 - 1
    assert to_score("l2", 0.2) == pytest.approx(-2.0)  # d**2 = 4
    assert to_score("l2", 1.0 + 1e-6) == pytest.approx(0.0)
    assert to_score("l2", 0.0) < -1e5  # never a division error
    for d in (0.0, 0.3, 1.0, 7.5):
        assert to_score("l2", 1.0 / (1.0 + d * d)) == pytest.approx(-d)
    for c in (-1.0, -0.2, 0.0, 0.7, 1.0):
        assert to_score("cosine", (1.0 + c) / 2.0) == pytest.approx(c)


def test_score_conversion_is_monotone() -> None:
    cos = [to_score("cosine", s / 100) for s in range(101)]
    l2 = [to_score("l2", s / 100) for s in range(1, 101)]
    assert cos == sorted(cos) and l2 == sorted(l2)


# ---- index definition helpers ---------------------------------------------------------------


def test_index_definition_round_trip() -> None:
    d = index_definition(8, "l2", ["year", "lang", "lang"])
    assert d == {
        "fields": [
            {"type": "vector", "path": VECTOR_FIELD, "numDimensions": 8, "similarity": "euclidean"},
            {"type": "filter", "path": "lang"},
            {"type": "filter", "path": "year"},
        ]
    }
    assert parse_index_definition(d) == (8, "euclidean", {"lang", "year"})
    assert parse_index_definition({}) == (0, "", set())


def _row(status: str = "READY", queryable: bool = True, latest: int = 1, main: int | None = 1) -> dict[str, Any]:
    detail = [] if main is None else [{"status": status, "mainIndex": {"definitionVersion": {"version": main}}}]
    return {
        "status": status,
        "queryable": queryable,
        "latestDefinitionVersion": {"version": latest},
        "statusDetail": detail,
    }


def test_index_state() -> None:
    assert index_state(_row()) == "ready"
    assert index_state(_row(main=None)) == "ready"  # a deployment that reports no per-host detail
    assert index_state(_row(status="BUILDING")) == "building"
    assert index_state(_row(queryable=False)) == "building"
    assert index_state(_row(status="PENDING", queryable=False)) == "building"
    assert index_state(_row(latest=2, main=1)) == "building"  # old definition still queryable while rebuilding
    assert index_state(_row(status="FAILED", queryable=False)) == "failed"
    assert index_state({"status": "READY", "queryable": True}) == "ready"


# ---- a fake pymongo ---------------------------------------------------------------------------


class FakeCollection:
    """The slice of ``pymongo.collection.Collection`` the adapter uses, with Atlas search semantics."""

    def __init__(self, owner: FakeDB, name: str, pm: Any) -> None:
        self.owner, self.name, self.pm = owner, name, pm
        self.docs: dict[str, dict[str, Any]] = {}
        self.indexes: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.pipelines: list[list[dict[str, Any]]] = []

    # plain collection calls
    def find_one(self, flt: dict[str, Any]) -> dict[str, Any] | None:
        with self.lock:
            doc = self.docs.get(flt["_id"])
            return dict(doc) if doc else None

    def replace_one(self, flt: dict[str, Any], doc: dict[str, Any], upsert: bool = False) -> None:
        with self.lock:
            self.docs[flt["_id"]] = dict(doc)

    def delete_one(self, flt: dict[str, Any]) -> None:
        with self.lock:
            doc = self.docs.get(flt["_id"])
            if doc is not None and all(doc.get(k) == v for k, v in flt.items() if k != "_id"):
                del self.docs[flt["_id"]]

    def delete_many(self, flt: dict[str, Any]) -> None:
        with self.lock:
            for i in flt["_id"]["$in"]:
                self.docs.pop(i, None)

    def update_one(self, flt: dict[str, Any], update: dict[str, Any], upsert: bool = False) -> None:
        with self.lock:
            doc = self.docs.get(flt["_id"])
            if doc is None:
                self.docs[flt["_id"]] = {"_id": flt["_id"], **update["$set"]}
            elif doc["expires"] < flt["expires"]["$lt"]:
                doc.update(update["$set"])
            else:
                raise self.pm.errors.DuplicateKeyError("E11000 duplicate key")

    def bulk_write(self, ops: list[Any], ordered: bool = True) -> None:
        with self.lock:
            for op in ops:
                self.docs[op._filter["_id"]] = dict(op._doc)

    def estimated_document_count(self) -> int:
        return len(self.docs)

    # search indexes
    def create_search_index(self, model: Any) -> None:
        doc = model.document
        assert doc["type"] == "vectorSearch"
        with self.lock:
            if doc["name"] in self.indexes:
                raise self.pm.errors.OperationFailure("Index already exists with a different definition", code=68)
            self.indexes[doc["name"]] = {"name": doc["name"], "definition": doc["definition"], "version": 0, "polls": 0}

    def update_search_index(self, name: str, definition: dict[str, Any]) -> None:
        with self.lock:
            idx = self.indexes[name]
            idx.update(definition=definition, version=idx["version"] + 1, polls=0)

    def list_search_indexes(self, name: str | None = None) -> Iterator[dict[str, Any]]:
        with self.lock:
            rows = []
            for idx in self.indexes.values():
                if name not in (None, idx["name"]):
                    continue
                idx["polls"] += 1
                built = idx["polls"] > self.owner.build_polls
                version = idx["version"] if built else max(idx["version"] - 1, 0)
                rows.append(
                    {
                        "name": idx["name"],
                        "numDocs": len(self.docs),
                        "status": "READY" if built else "BUILDING",
                        "queryable": built or idx["version"] > 0,  # an updated index keeps serving the old one
                        "latestDefinition": idx["definition"],
                        "latestDefinitionVersion": {"version": idx["version"]},
                        "statusDetail": [{"mainIndex": {"definitionVersion": {"version": version}}}],
                    }
                )
            return iter(rows)

    # $vectorSearch
    def aggregate(self, pipeline: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        self.pipelines.append(pipeline)
        stage = pipeline[0]["$vectorSearch"]
        err = self.pm.errors.OperationFailure
        with self.lock:
            idx = self.indexes.get(stage["index"])
            if idx is None:
                return iter([])
            dim, similarity, paths = parse_index_definition(idx["definition"])
            q = stage["queryVector"]
            if len(q) != dim:
                raise err(f"vector field is indexed with {dim} dimensions but queried with {len(q)}")
            if stage.get("exact"):
                assert "numCandidates" not in stage
            elif not 1 <= stage["numCandidates"] <= 10000 or stage["limit"] > stage["numCandidates"]:
                raise err("numCandidates out of bounds")
            flt = stage.get("filter")
            if flt is not None:
                for path in _paths(flt):
                    if path not in paths:
                        raise err(f"Path '{path}' needs to be indexed as filter")
            scored = []
            for doc in self.docs.values():
                v = doc[VECTOR_FIELD]
                if flt is not None and not _mql_matches(flt, doc):
                    continue
                if similarity == "cosine":
                    na, nb = math.sqrt(sum(x * x for x in v)), math.sqrt(sum(x * x for x in q))
                    s = (1 + sum(x * y for x, y in zip(v, q, strict=True)) / (na * nb)) / 2
                else:
                    s = 1 / (1 + sum((x - y) ** 2 for x, y in zip(v, q, strict=True)))
                scored.append({"_id": doc["_id"], "s": s})
            scored.sort(key=lambda r: -r["s"])
            return iter(scored[: stage["limit"]])


def _paths(expr: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key, body in expr.items():
        if key in ("$and", "$or"):
            out.extend(p for e in body for p in _paths(e))
        else:
            out.append(key)
    return out


class FakeDB:
    def __init__(self, pm: Any, build_polls: int) -> None:
        self.pm, self.build_polls = pm, build_polls
        self.collections: dict[str, FakeCollection] = {}
        self.lock = threading.RLock()

    def __getitem__(self, name: str) -> FakeCollection:
        with self.lock:
            return self.collections.setdefault(name, FakeCollection(self, name, self.pm))

    def create_collection(self, name: str) -> None:
        with self.lock:
            if name in self.collections and (self.collections[name].docs or self.collections[name].indexes):
                raise self.pm.errors.CollectionInvalid(f"collection {name} already exists")
            self.collections.setdefault(name, FakeCollection(self, name, self.pm))

    def drop_collection(self, name: str) -> None:
        with self.lock:
            self.collections.pop(name, None)


class FakeClient:
    def __init__(self, build_polls: int = 0, list_error: Exception | None = None) -> None:
        pm = pytest.importorskip("pymongo")
        self.dbs: dict[str, FakeDB] = {}
        self.build_polls = build_polls
        self.pm = pm
        self.list_error = list_error
        self.closed = False
        self.admin = self
        self.pinged = 0

    def __getitem__(self, name: str) -> FakeDB:
        return self.dbs.setdefault(name, FakeDB(self.pm, self.build_polls))

    def command(self, name: str) -> dict[str, Any]:
        assert name == "ping"
        self.pinged += 1
        return {"ok": 1}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


def _spec(name: str = "rc_fake_0001", dim: int = 4, metric: Any = "cosine", **fields: FieldType) -> CollectionSpec:
    return CollectionSpec(name, dim, metric, fields or {"lang": "str", "year": "int"})


def test_catalog_round_trip_and_layout(fake: FakeClient) -> None:
    store = MongoDBStore(fake, database="d", prefix="p_")
    store.ensure_collection(_spec())
    db = fake["d"]
    assert set(db.collections) == {CATALOG_COLLECTION, "p_rc_fake_0001"}  # prefixed physical name
    entry_doc = db[CATALOG_COLLECTION].docs["p_rc_fake_0001"]
    assert set(entry_doc) == {"_id", "entry"}
    import json

    entry = json.loads(entry_doc["entry"])
    assert entry["dim"] == 4 and entry["metric"] == "cosine"
    assert entry["fields"] == {"lang": "str", "year": "int", "document_id": "str"}
    assert entry["options"] == {"layout": 1, "vector_field": "embedding"}
    assert "uri" not in json.dumps(entry)
    idx = db["p_rc_fake_0001"].indexes[INDEX_NAME]
    assert parse_index_definition(idx["definition"]) == (4, "cosine", {"lang", "year", "document_id"})
    info = store.collection_info("rc_fake_0001")
    assert info is not None and (info.dim, info.metric, info.count) == (4, "cosine", 0)
    assert not [k for k in db[CATALOG_COLLECTION].docs if k.startswith("lock:")]  # the lease is released
    store.ensure_collection(_spec())  # idempotent
    store.drop_collection("rc_fake_0001")
    assert store.collection_info("rc_fake_0001") is None
    assert "p_rc_fake_0001" not in db.collections and not db[CATALOG_COLLECTION].docs
    store.drop_collection("rc_fake_0001")
    store.close()
    assert fake.closed


def test_catalog_mismatches(fake: FakeClient) -> None:
    store = MongoDBStore(fake)
    store.ensure_collection(_spec(lang="str"))
    with pytest.raises(CollectionMismatch):
        store.ensure_collection(_spec(dim=5, lang="str"))
    with pytest.raises(CollectionMismatch):
        store.ensure_collection(_spec(metric="l2", lang="str"))
    with pytest.raises(CollectionMismatch):
        store.ensure_collection(_spec(lang="int"))
    store.ensure_collection(_spec(lang="str", year="int"))  # additive
    # the catalog agrees but the index does not (changed behind our back): refuse, do not guess
    fake["ragcentre"]["rc_fake_0001"].indexes[INDEX_NAME]["definition"] = index_definition(9, "cosine", ["lang"])
    with pytest.raises(CollectionMismatch, match="dimension 9"):
        store.ensure_collection(_spec(lang="str", year="int"))


def test_adding_a_field_updates_the_index_and_waits(fake: FakeClient) -> None:
    fake.build_polls = 3
    store = MongoDBStore(fake)
    store.ensure_collection(_spec(lang="str"))
    idx = fake["ragcentre"]["rc_fake_0001"].indexes[INDEX_NAME]
    assert idx["polls"] > 3  # waited for the build
    store.ensure_collection(_spec(lang="str", year="int"))
    assert idx["version"] == 1
    assert parse_index_definition(idx["definition"])[2] == {"lang", "year", "document_id"}
    assert idx["polls"] > 3  # waited for the rebuild, though the old index was queryable all along
    store.ensure_collection(_spec(lang="str", year="int"))
    assert idx["version"] == 1  # nothing to do


def test_a_catalog_ahead_of_its_index_is_repaired(fake: FakeClient) -> None:
    store = MongoDBStore(fake)
    store.ensure_collection(_spec(lang="str"))
    store._catalog.ensure(
        _spec(lang="str", year="int", document_id="str"), store._options()
    )  # crash before the index update
    store.ensure_collection(_spec(lang="str", year="int"))
    idx = fake["ragcentre"]["rc_fake_0001"].indexes[INDEX_NAME]
    assert parse_index_definition(idx["definition"])[2] == {"lang", "year", "document_id"}


def test_an_index_whose_catalog_was_lost_is_adopted_when_it_matches(fake: FakeClient) -> None:
    store = MongoDBStore(fake)
    store.ensure_collection(_spec(lang="str"))
    fake["ragcentre"][CATALOG_COLLECTION].docs.clear()
    assert store.collection_info("rc_fake_0001") is None
    store.ensure_collection(_spec(lang="str"))
    info = store.collection_info("rc_fake_0001")
    assert info is not None and info.dim == 4
    fake["ragcentre"][CATALOG_COLLECTION].docs.clear()
    with pytest.raises(CollectionMismatch):
        store.ensure_collection(_spec(dim=6, lang="str"))


def test_a_stuck_lock_expires_and_a_held_lock_blocks(fake: FakeClient) -> None:
    store = MongoDBStore(fake, index_wait=0.4)
    meta = fake["ragcentre"][CATALOG_COLLECTION]
    meta.docs["lock:rc_fake_0001"] = {"_id": "lock:rc_fake_0001", "owner": "dead", "expires": time.time() - 1}
    store.ensure_collection(_spec())  # an expired lease is taken over
    meta.docs["lock:rc_fake_0001"] = {"_id": "lock:rc_fake_0001", "owner": "alive", "expires": time.time() + 60}
    store2 = MongoDBStore(fake, index_wait=0.0)
    mod.LOCK_LEASE_SECONDS, saved = 0.3, mod.LOCK_LEASE_SECONDS
    try:
        with pytest.raises(StoreError, match="timed out waiting"):
            store2.drop_collection("rc_fake_0001")
    finally:
        mod.LOCK_LEASE_SECONDS = saved


def test_concurrent_ensure_from_separate_stores_has_one_creator(fake: FakeClient) -> None:
    fake.build_polls = 2
    errors: list[BaseException] = []

    def work() -> None:
        try:
            MongoDBStore(fake).ensure_collection(_spec(lang="str"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    assert not errors, errors
    assert len(fake["ragcentre"]["rc_fake_0001"].indexes) == 1


def test_build_wait_times_out_and_reports_failure(fake: FakeClient) -> None:
    fake.build_polls = 10**9
    with pytest.raises(StoreError, match="not ready after"):
        MongoDBStore(fake, index_wait=0.3).ensure_collection(_spec())
    fake2 = FakeClient()
    store = MongoDBStore(fake2)
    store.ensure_collection(_spec())
    idx = fake2["ragcentre"]["rc_fake_0001"].indexes[INDEX_NAME]
    idx["definition"] = index_definition(4, "cosine", ["lang", "year", "document_id"])
    real = FakeCollection.list_search_indexes

    def failed(self: FakeCollection, name: str | None = None) -> Iterator[dict[str, Any]]:
        return iter([{**row, "status": "FAILED", "queryable": False} for row in real(self, name)])

    FakeCollection.list_search_indexes = failed  # type: ignore[method-assign]
    try:
        with pytest.raises(StoreError, match="failed"):
            store._wait_ready("rc_fake_0001")
    finally:
        FakeCollection.list_search_indexes = real  # type: ignore[method-assign]


def test_healthcheck_explains_a_server_without_search(fake: FakeClient) -> None:
    store = MongoDBStore(fake)
    store.healthcheck()
    assert fake.pinged == 1

    def unsupported(self: FakeCollection, name: str | None = None) -> Iterator[dict[str, Any]]:
        raise fake.pm.errors.OperationFailure("Search index commands are only supported with Atlas", code=115)

    FakeCollection.list_search_indexes, saved = unsupported, FakeCollection.list_search_indexes  # type: ignore[method-assign,assignment]
    try:
        with pytest.raises(StoreError, match="does not support search indexes"):
            store.healthcheck()
    finally:
        FakeCollection.list_search_indexes = saved  # type: ignore[method-assign]


def _recs(n: int = 6, dim: int = 4) -> list[Record]:
    rng = random.Random(3)
    return [
        Record(str(uuid.UUID(int=rng.getrandbits(128), version=4)), random_vector(rng, dim),
               {"document_id": f"doc-{i}", "lang": "en" if i % 2 else "de", "year": 2000 + i})
        for i in range(n)
    ]  # fmt: skip


def test_query_builds_the_documented_stage(fake: FakeClient) -> None:
    store = MongoDBStore(fake, num_candidates_factor=7)
    store.ensure_collection(_spec())
    store.upsert("rc_fake_0001", _recs())
    coll = fake["ragcentre"]["rc_fake_0001"]
    f = _schema({"lang": "en"})
    store.query("rc_fake_0001", [1, 0, 0, 0], 3, f)
    stage = coll.pipelines[-1][0]["$vectorSearch"]
    assert stage == {
        "index": INDEX_NAME,
        "path": VECTOR_FIELD,
        "queryVector": [1.0, 0.0, 0.0, 0.0],
        "limit": 3,
        "numCandidates": 21,
        "filter": {"lang": {"$eq": "en"}},
    }
    assert coll.pipelines[-1][1] == {"$project": {"_id": 1, "s": {"$meta": "vectorSearchScore"}}}
    store.query("rc_fake_0001", [1, 0, 0, 0], 2000)  # factor x k beyond the cap: numCandidates stops at 10000
    assert (
        coll.pipelines[-1][0]["$vectorSearch"]["numCandidates"] == 10000
        and "filter" not in coll.pipelines[-1][0]["$vectorSearch"]
    )
    store.query("rc_fake_0001", [1, 0, 0, 0], 10001)  # k above the cap: exact search instead
    s = coll.pipelines[-1][0]["$vectorSearch"]
    assert s["exact"] is True and "numCandidates" not in s and s["limit"] == 10001
    exact = MongoDBStore(fake, exact=True)
    exact.query("rc_fake_0001", [1, 0, 0, 0], 3)
    s = coll.pipelines[-1][0]["$vectorSearch"]
    assert s["exact"] is True and "numCandidates" not in s
    assert store.query("rc_fake_0001", [1, 0, 0, 0], 0) == []


def test_validation(fake: FakeClient) -> None:
    store = MongoDBStore(fake)
    with pytest.raises(CollectionNotFound):
        store.query("rc_fake_0001", [1, 0, 0, 0], 3)
    with pytest.raises(CollectionNotFound):
        store.upsert("rc_fake_0001", _recs(1))
    store.upsert("rc_fake_0001", [])  # harmless
    store.delete("rc_fake_0001", ["x"])
    store.delete("rc_fake_0001", [])
    store.ensure_collection(_spec())
    rec = _recs(1)[0]
    with pytest.raises(StoreError, match="dimension 3"):
        store.upsert("rc_fake_0001", [Record(rec.id, [1.0, 2.0, 3.0], rec.metadata)])
    with pytest.raises(StoreError, match="dimension 3"):
        store.query("rc_fake_0001", [1, 2, 3], 1)
    with pytest.raises(StoreError, match="zero vector"):
        store.upsert("rc_fake_0001", [Record(rec.id, [0.0] * 4, rec.metadata)])
    with pytest.raises(StoreError, match="zero vector"):
        store.query("rc_fake_0001", [0, 0, 0, 0], 1)
    with pytest.raises(StoreError, match="NaN"):
        store.upsert("rc_fake_0001", [Record(rec.id, [math.nan, 1, 1, 1], rec.metadata)])
    with pytest.raises(StoreError, match="NaN"):
        store.query("rc_fake_0001", [math.inf, 1, 1, 1], 1)
    store.upsert("rc_fake_0001", [Record(rec.id, rec.vector, {**rec.metadata, "extra": 1})])
    assert "extra" not in fake["ragcentre"]["rc_fake_0001"].docs[rec.id]  # undeclared: stored nowhere
    with pytest.raises(StoreError, match="must be int"):
        store.upsert("rc_fake_0001", [Record(rec.id, rec.vector, {**rec.metadata, "year": "x"})])
    with pytest.raises(StoreError, match="must be int"):
        store.upsert("rc_fake_0001", [Record(rec.id, rec.vector, {**rec.metadata, "year": True})])
    with pytest.raises(StoreError, match="8000 are not matched"):
        store.upsert("rc_fake_0001", [Record(rec.id, rec.vector, {**rec.metadata, "lang": "x" * 8001})])
    store.upsert("rc_fake_0001", [Record(rec.id, rec.vector, {**rec.metadata, "lang": "x" * 8000})])
    with pytest.raises(StoreError, match="exceeds max_batch"):
        store.upsert("rc_fake_0001", _recs(1) * 501)
    with pytest.raises(StoreError, match="not a filterable field"):
        store.query("rc_fake_0001", [1, 0, 0, 0], 1, Cond("nope", "$eq", 1))
    with pytest.raises(StoreError, match="invalid collection name"):
        store.query("Bad Name", [1, 0, 0, 0], 1)
    with pytest.raises(StoreError):
        store.ensure_collection(_spec(dim=8193))
    with pytest.raises(StoreError, match="metric"):
        store.ensure_collection(_spec(metric="dot"))
    for reserved in ("_id", "embedding", "a.b", "$x"):
        with pytest.raises(StoreError, match="reserved or not a valid"):
            store.ensure_collection(CollectionSpec("rc_fake_0002", 4, "cosine", {reserved: "str"}))
    # a repeated id in one batch: the last wins
    a = _recs(1)[0]
    store.upsert("rc_fake_0001", [a, Record(a.id, [0.0, 1.0, 0.0, 0.0], {**a.metadata, "lang": "zz"})])
    assert fake["ragcentre"]["rc_fake_0001"].docs[a.id]["lang"] == "zz"


def test_count_is_what_queries_can_see(fake: FakeClient) -> None:
    store = MongoDBStore(fake)
    store.ensure_collection(_spec())
    store.upsert("rc_fake_0001", _recs(6))
    coll = fake["ragcentre"]["rc_fake_0001"]
    info = store.collection_info("rc_fake_0001")
    assert info is not None and info.count == 6
    assert coll.pipelines[-1][0]["$vectorSearch"]["exact"] is True  # confirmed by an exact search
    # an index that reports more documents than the searcher shows yet: the lower number wins
    real = FakeCollection.aggregate

    def lagging(self: FakeCollection, pipeline: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        return iter(list(real(self, pipeline))[:4])

    FakeCollection.aggregate = lagging  # type: ignore[method-assign]
    try:
        info = store.collection_info("rc_fake_0001")
    finally:
        FakeCollection.aggregate = real  # type: ignore[method-assign]
    assert info is not None and info.count == 4
    mod.VISIBLE_PROBE_LIMIT, saved = 3, mod.VISIBLE_PROBE_LIMIT  # beyond the probe limit: the index's own count
    try:
        n = len(coll.pipelines)
        info = store.collection_info("rc_fake_0001")
        assert info is not None and info.count == 6 and len(coll.pipelines) == n
    finally:
        mod.VISIBLE_PROBE_LIMIT = saved


def test_the_reserved_catalog_name_cannot_be_a_collection(fake: FakeClient) -> None:
    store = MongoDBStore(fake, prefix="_ragcentre_")
    with pytest.raises(StoreError, match="reserved"):
        store.ensure_collection(_spec(name="collections"))


def test_prefix_isolates_collections(fake: FakeClient) -> None:
    a, b = MongoDBStore(fake, prefix="a_"), MongoDBStore(fake, prefix="b_")
    a.ensure_collection(_spec())
    assert b.collection_info("rc_fake_0001") is None
    b.ensure_collection(_spec(dim=6))
    info_a, info_b = a.collection_info("rc_fake_0001"), b.collection_info("rc_fake_0001")
    assert info_a is not None and info_b is not None and (info_a.dim, info_b.dim) == (4, 6)
    a.drop_collection("rc_fake_0001")
    assert b.collection_info("rc_fake_0001") is not None


class TestMongoDBAgainstTheFake(VectorStoreContract):
    """The whole contract suite on the in-memory stand-in: adapter plumbing, catalog, filters, scores."""

    @pytest.fixture
    def store(self) -> Iterator[MongoDBStore]:
        s = MongoDBStore(FakeClient(build_polls=1), exact=True, index_wait=20)
        yield s
        s.close()


class TestMongoDBAgainstTheFakeApproximate(VectorStoreContract):
    @pytest.fixture
    def store(self) -> Iterator[MongoDBStore]:
        s = MongoDBStore(FakeClient(), exact=False, index_wait=20)
        yield s
        s.close()


# ---- live: MongoDB Atlas Local / Atlas -------------------------------------------------------


def _reachable() -> str | None:
    pymongo = pytest.importorskip("pymongo")
    client = pymongo.MongoClient(URI, serverSelectionTimeoutMS=2500, appname="ragcentre-test")
    try:
        client.admin.command("ping")
        list(client["rctest_probe"]["probe"].list_search_indexes())
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        client.close()
    return None


@pytest.fixture(scope="module")
def server() -> str:
    pytest.importorskip("pymongo")
    problem = _reachable()
    if problem:
        pytest.skip(f"MongoDB with search indexes is not reachable at the test URI ({problem})")
    return URI


class _Opened:
    """Opens stores on one fresh database, and removes it afterwards."""

    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.database = f"rctest_{uuid.uuid4().hex[:10]}"
        self.stores: list[MongoDBStore] = []

    def open(self, **kw: Any) -> MongoDBStore:
        kw.setdefault("database", self.database)
        store = MongoDBStore.connect(uri=self.uri, **kw)
        store._canary = threading.local()  # type: ignore[attr-defined]
        _track(store)
        self.stores.append(store)
        return store

    def close(self) -> None:
        pymongo = __import__("pymongo")
        client = pymongo.MongoClient(self.uri, serverSelectionTimeoutMS=5000)
        try:
            client.drop_database(self.database)
        finally:
            client.close()
            for s in self.stores:
                s.close()


def _track(store: MongoDBStore) -> None:
    """Remember each thread's last written record per collection: ``settle`` uses it as a canary."""
    upsert, delete, drop = store.upsert, store.delete, store.drop_collection

    def tracked_upsert(name: str, records: Any) -> None:
        upsert(name, records)
        if records:
            store._canary.rec = {**getattr(store._canary, "rec", {}), name: records[-1]}  # type: ignore[attr-defined]

    def tracked_delete(name: str, ids: Any) -> None:
        delete(name, ids)
        rec = getattr(store._canary, "rec", {})
        if name in rec and rec[name].id in set(ids):
            store._canary.rec = {k: v for k, v in rec.items() if k != name}  # type: ignore[attr-defined]

    def tracked_drop(name: str) -> None:
        drop(name)
        store._canary.rec = {k: v for k, v in getattr(store._canary, "rec", {}).items() if k != name}  # type: ignore[attr-defined]

    store.upsert, store.delete, store.drop_collection = tracked_upsert, tracked_delete, tracked_drop  # type: ignore[method-assign]


def wait_visible(store: MongoDBStore, name: str, timeout: float = 90.0) -> None:
    """Wait until every write made so far is visible to ``$vectorSearch``.

    Search indexes are eventually consistent: the index follows the collection's change stream. A
    write is visible when the index has built its latest definition, its document count equals the
    collection's (deletes), and a query for this thread's last written record finds it with all its
    metadata. Unordered bulk writes can make the last record searchable before earlier records,
    so the searchable count is checked as well as the last-written record.
    """
    if store.collection_info(name) is None:
        return
    physical = store._physical(name)
    canary = getattr(store._canary, "rec", {}).get(name)  # type: ignore[attr-defined]

    def count_matches() -> bool:
        idx = store._index(physical)
        if idx is None or index_state(idx) != "ready":
            return False
        docs = idx.get("numDocs")
        before = store._db[physical].estimated_document_count()
        if docs is not None and docs != before:
            return False
        entry = store._catalog.get(name)
        assert entry is not None
        searchable = store._visible(physical, entry.dim, before) if before else 0
        return searchable == before == store._db[physical].estimated_document_count()

    def canary_found() -> bool:
        if canary is None:
            return True
        declared = store._catalog.get(name).fields  # type: ignore[union-attr]
        items = tuple(Cond(k, "$eq", v) for k, v in canary.metadata.items() if k in declared)
        got = store.query(name, canary.vector, 1, items[0] if len(items) == 1 else And(items))
        return bool(got) and got[0].id == canary.id

    wait_until(
        lambda: count_matches() and canary_found(), timeout=timeout, interval=0.1, what=f"{name} to be searchable"
    )


@pytest.fixture
def opened(server: str) -> Iterator[_Opened]:
    o = _Opened(server)
    try:
        yield o
    finally:
        o.close()


class _LiveContract(VectorStoreContract):
    """Subclasses set ``options``."""

    options: ClassVar[dict[str, Any]] = {}

    @pytest.fixture
    def store(self, opened: _Opened) -> MongoDBStore:
        return opened.open(**self.options)

    def settle(self, store: VectorStore, name: str) -> None:
        wait_visible(store, name)  # type: ignore[arg-type]


class TestMongoDBApproximateContract(_LiveContract):
    """Default configuration: approximate search. Filtered queries are held to recall >= 0.9."""

    options: ClassVar[dict[str, Any]] = {"exact": False}


class TestMongoDBExactContract(_LiveContract):
    """``exact=true`` (ENN): exact top-k including every filtered query, and ``filter_exact``."""

    options: ClassVar[dict[str, Any]] = {"exact": True}

    def test_declares_exact_filtering(self, store: MongoDBStore) -> None:
        assert store.capabilities.filter_exact


class TestMongoDBPrefixedContract(_LiveContract):
    options: ClassVar[dict[str, Any]] = {
        "exact": False,
        "num_candidates_factor": 50,
        "prefix": "pre_",
    }  # also covers the prefix path


def test_approximate_declares_filtered_search_may_be_inexact(opened: _Opened) -> None:
    assert not opened.open().capabilities.filter_exact
    assert opened.open(exact=True).capabilities.filter_exact


def test_healthcheck_and_is_a_vector_store(opened: _Opened) -> None:
    store = opened.open()
    store.healthcheck()
    assert isinstance(store, VectorStore)


def test_layout_in_mongodb(opened: _Opened) -> None:
    store = opened.open(prefix="px_")
    store.ensure_collection(_spec(dim=4, lang="str", year="int"))
    recs = _recs(3)
    store.upsert("rc_fake_0001", recs)
    wait_visible(store, "rc_fake_0001")
    db = store._db
    assert set(db.list_collection_names()) == {CATALOG_COLLECTION, "px_rc_fake_0001"}
    doc = db["px_rc_fake_0001"].find_one({"_id": recs[0].id})
    assert doc is not None
    assert set(doc) == {"_id", "embedding", "document_id", "lang", "year"}
    assert doc["embedding"] == pytest.approx(recs[0].vector)
    (idx,) = list(db["px_rc_fake_0001"].list_search_indexes())
    assert idx["name"] == INDEX_NAME and idx["type"] == "vectorSearch"
    assert parse_index_definition(idx["latestDefinition"]) == (4, "cosine", {"document_id", "lang", "year"})
    assert idx["latestDefinition"]["fields"][0] == {
        "type": "vector", "path": "embedding", "numDimensions": 4, "similarity": "cosine",
    }  # fmt: skip
    assert db[CATALOG_COLLECTION].find_one({"_id": "px_rc_fake_0001"})["entry"].startswith("{")
    assert db[CATALOG_COLLECTION].count_documents({"_id": {"$regex": "^lock:"}}) == 0  # lease released


def test_scores_match_the_mathematical_definitions(opened: _Opened) -> None:
    """The formulas ``(1+cos)/2`` and ``1/(1+d**2)`` that the conversion inverts, on hand-made vectors."""
    q = [3.0, 4.0]
    for metric, vecs in (
        ("cosine", [[3.0, 4.0], [4.0, 3.0], [-4.0, 3.0], [-3.0, -4.0]]),
        ("l2", [[3.0, 4.0], [1.0, 2.0], [4.0, 5.0], [30.0, 40.0]]),
    ):
        store = opened.open(exact=True)
        name = f"rc_scale_{metric[:2]}"
        store.ensure_collection(CollectionSpec(name, 2, metric, {}))  # type: ignore[arg-type]
        ids = [str(uuid.UUID(int=i + 1)) for i in range(len(vecs))]
        store.upsert(name, [Record(i, v, {"document_id": "d"}) for i, v in zip(ids, vecs, strict=True)])
        wait_visible(store, name)
        got = {m.id: m.score for m in store.query(name, q, 10)}
        assert len(got) == len(vecs)
        for rid, v in zip(ids, vecs, strict=True):
            assert got[rid] == pytest.approx(brute_score(metric, v, q), abs=1e-4), (metric, v)  # type: ignore[arg-type]
        if metric == "cosine":
            assert got[ids[0]] == pytest.approx(1.0, abs=1e-5) and got[ids[3]] == pytest.approx(-1.0, abs=1e-5)
        else:
            assert got[ids[1]] == pytest.approx(-math.sqrt(8), abs=1e-4) and got[ids[2]] == pytest.approx(
                -math.sqrt(2), abs=1e-4
            )


def test_missing_fields_follow_the_reference_evaluator(opened: _Opened) -> None:
    """Documents that lack a field (a schema field added later) match $ne and $nin only, as in ``matches``."""
    store = opened.open(exact=True)
    spec = CollectionSpec("rc_missing_0001", DIM, "cosine", dict(SCHEMA_FIELDS))
    store.ensure_collection(spec)
    rng = random.Random(11)
    records = make_records(rng, 60)
    store.upsert("rc_missing_0001", records)
    wait_visible(store, "rc_missing_0001")
    physical = store._physical("rc_missing_0001")
    stripped: dict[str, dict[str, Any]] = {}
    for i, rec in enumerate(records):
        drop = [f for j, f in enumerate(sorted(SCHEMA_FIELDS)) if (i + j) % 3 == 0] + (
            ["document_id"] if i % 7 == 0 else []
        )
        if drop:
            store._db[physical].update_one({"_id": rec.id}, {"$unset": {f: "" for f in drop}})
        stripped[rec.id] = {k: v for k, v in rec.metadata.items() if k not in drop}
    wait_until(lambda: True, timeout=1)
    time.sleep(2.0)  # let the index apply the unsets
    schema = full_schema()
    probes = [
        {"lang": {"$ne": "en"}}, {"lang": {"$nin": ["en", "de"]}}, {"year": {"$ne": 2020}}, {"public": {"$ne": True}},
        {"score": {"$nin": [0.5, 1]}}, {"lang": "en"}, {"year": {"$gte": 2020}}, {"public": False},
        {"$or": [{"lang": {"$ne": "en"}}, {"year": {"$lt": 2018}}]}, {"document_id": {"$ne": "doc-01"}},
        {"$and": [{"lang": {"$nin": ["fr"]}}, {"public": {"$ne": False}}]},
    ]  # fmt: skip
    for raw in probes:
        f = parse_filter(raw, schema)
        got = {m.id for m in store.query("rc_missing_0001", random_vector(rng, DIM), 100, f)}
        assert got == {rid for rid, md in stripped.items() if matches(f, md)}, raw


def test_adding_a_field_to_a_populated_index(opened: _Opened) -> None:
    store = opened.open(exact=True)
    store.ensure_collection(CollectionSpec("rc_grow_0001", DIM, "cosine", {"lang": "str"}))
    rng = random.Random(5)
    records = make_records(rng, 80)
    store.upsert("rc_grow_0001", records)
    wait_visible(store, "rc_grow_0001")
    with pytest.raises(StoreError, match="not a filterable field"):
        store.query("rc_grow_0001", random_vector(rng, DIM), 5, parse_filter({"year": {"$gte": 2020}}, full_schema()))
    store.ensure_collection(CollectionSpec("rc_grow_0001", DIM, "cosine", {"lang": "str", "year": "int"}))
    q = random_vector(rng, DIM)
    f = parse_filter({"year": {"$gte": 2020}}, full_schema())
    # Documents written before the field existed did not store it, so none match yet ...
    assert store.query("rc_grow_0001", q, 100, f) == []
    store.upsert("rc_grow_0001", records)  # ... until they are rewritten, which adding a schema field schedules
    wait_visible(store, "rc_grow_0001")
    got = store.query("rc_grow_0001", q, 100, f)
    assert {m.id for m in got} == {r.id for r in records if matches(f, r.metadata)}


def test_recovers_from_a_half_created_collection(opened: _Opened) -> None:
    store = opened.open()
    spec = CollectionSpec("rc_half_0001", DIM, "l2", dict(SCHEMA_FIELDS))
    _, normalised = store._prepare(spec)
    store._catalog.ensure(normalised, store._options())  # the catalog was written; the index never was
    assert store.collection_info("rc_half_0001") is None
    store.ensure_collection(spec)
    info = store.collection_info("rc_half_0001")
    assert info is not None and (info.dim, info.metric) == (DIM, "l2")


def test_adopts_an_index_whose_catalog_entry_was_lost_and_refuses_a_different_one(opened: _Opened) -> None:
    store = opened.open()
    spec = CollectionSpec("rc_adopt_0001", DIM, "cosine", dict(SCHEMA_FIELDS))
    store.ensure_collection(spec)
    store._meta.delete_one({"_id": "rc_adopt_0001"})
    assert store.collection_info("rc_adopt_0001") is None
    store.ensure_collection(spec)
    assert store.collection_info("rc_adopt_0001") is not None
    store._meta.delete_one({"_id": "rc_adopt_0001"})
    with pytest.raises(CollectionMismatch):
        store.ensure_collection(CollectionSpec("rc_adopt_0001", DIM + 1, "cosine", dict(SCHEMA_FIELDS)))


def test_concurrent_ensure_from_separate_stores(opened: _Opened) -> None:
    spec = CollectionSpec("rc_race_0001", DIM, "cosine", dict(SCHEMA_FIELDS))
    stores = [opened.open() for _ in range(5)]
    errors: list[BaseException] = []

    def work(s: MongoDBStore) -> None:
        try:
            s.ensure_collection(spec)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(s,)) for s in stores]
    [t.start() for t in threads]
    [t.join(180) for t in threads]
    assert not errors, errors
    assert len(list(stores[0]._db[stores[0]._physical("rc_race_0001")].list_search_indexes())) == 1
    info = stores[0].collection_info("rc_race_0001")
    assert info is not None and info.dim == DIM


def test_drop_and_recreate_with_another_shape_right_away(opened: _Opened) -> None:
    store = opened.open()
    store.ensure_collection(CollectionSpec("rc_redo_0001", DIM, "cosine", dict(SCHEMA_FIELDS)))
    store.upsert("rc_redo_0001", make_records(random.Random(1), 20))
    store.drop_collection("rc_redo_0001")
    store.ensure_collection(CollectionSpec("rc_redo_0001", DIM + 3, "l2", dict(SCHEMA_FIELDS)))
    info = store.collection_info("rc_redo_0001")
    assert info is not None and (info.dim, info.metric, info.count) == (DIM + 3, "l2", 0)


def test_large_dimension_and_limits(opened: _Opened) -> None:
    store = opened.open()
    store.ensure_collection(CollectionSpec("rc_big_0001", 8192, "cosine", {}))  # the server's own cap
    rng = random.Random(2)
    vecs = [random_vector(rng, 8192) for _ in range(3)]
    store.upsert(
        "rc_big_0001", [Record(str(uuid.UUID(int=i + 1)), v, {"document_id": "d"}) for i, v in enumerate(vecs)]
    )
    wait_visible(store, "rc_big_0001")
    got = store.query("rc_big_0001", vecs[1], 3)
    assert got[0].id == str(uuid.UUID(int=2)) and got[0].score == pytest.approx(1.0, abs=1e-4)
    with pytest.raises(StoreError, match="at most 8192"):
        store.ensure_collection(CollectionSpec("rc_big_0002", 8193, "cosine", {}))
    assert store.collection_info("rc_big_0002") is None


def test_strings_beyond_the_reliable_length_are_rejected_not_lost(opened: _Opened) -> None:
    store = opened.open(exact=True)
    store.ensure_collection(CollectionSpec("rc_str_0001", 2, "cosine", {"lang": "str"}))
    ok = "é" * 4000  # 8000 bytes, 4000 characters: indexed and matched exactly
    store.upsert("rc_str_0001", [Record(str(uuid.UUID(int=1)), [1.0, 0.0], {"document_id": "d", "lang": ok})])
    wait_visible(store, "rc_str_0001")
    got = store.query("rc_str_0001", [1.0, 0.1], 5, parse_filter({"lang": ok}, full_schema()))
    assert [m.id for m in got] == [str(uuid.UUID(int=1))]
    with pytest.raises(StoreError, match="not matched reliably"):
        store.upsert(
            "rc_str_0001", [Record(str(uuid.UUID(int=2)), [1.0, 0.0], {"document_id": "d", "lang": "y" * 8001})]
        )


def test_hostile_string_values_round_trip(opened: _Opened) -> None:
    store = opened.open(exact=True)
    store.ensure_collection(CollectionSpec("rc_host_0001", 2, "cosine", {"lang": "str"}))
    values = [
        "",
        " ",
        "a b",
        "$gt",
        "a.b",
        '{"$ne": 1}',
        "it's",
        'say "hi"',
        "back\\slash",
        "ünï ☃ 日本語 🙂",
        "EN",
        "en",
        "\t\n",
    ]
    rng = random.Random(4)
    recs = [
        Record(str(uuid.UUID(int=i + 1)), [rng.uniform(-1, 1), rng.uniform(-1, 1) + 2], {"document_id": "d", "lang": v})
        for i, v in enumerate(values)
    ]
    store.upsert("rc_host_0001", recs)
    wait_visible(store, "rc_host_0001")
    for rec in recs:
        f = parse_filter({"lang": rec.metadata["lang"]}, full_schema())
        got = {m.id for m in store.query("rc_host_0001", [0.3, 1.0], 50, f)}
        assert got == {r.id for r in recs if matches(f, r.metadata)}, rec.metadata["lang"]


def test_numeric_edges(opened: _Opened) -> None:
    store = opened.open(exact=True)
    store.ensure_collection(CollectionSpec("rc_num_0001", 2, "cosine", {"year": "int", "score": "float"}))
    values = [(-5, -0.5), (0, 0.0), (2**40, 1.0), (7, 1e-9), (2**53 - 1, 1e12)]
    recs = [
        Record(str(uuid.UUID(int=i + 1)), [1.0, 0.1 * (i + 1)], {"document_id": "d", "year": y, "score": s})
        for i, (y, s) in enumerate(values)
    ]
    store.upsert("rc_num_0001", recs)
    wait_visible(store, "rc_num_0001")
    for raw in (
        {"year": 2**40},
        {"year": {"$lt": 0}},
        {"year": {"$gte": 7}},
        {"score": 1},
        {"score": {"$gt": 0.0}},
        {"score": {"$lte": 1e-9}},
        {"year": {"$in": [0, 7]}},
    ):
        f = parse_filter(raw, full_schema())
        got = {m.id for m in store.query("rc_num_0001", [1.0, 0.5], 10, f)}
        assert got == {r.id for r in recs if matches(f, r.metadata)}, raw


def test_zero_and_dimension_checks_happen_before_the_server(opened: _Opened) -> None:
    store = opened.open()
    store.ensure_collection(CollectionSpec("rc_chk_0001", DIM, "cosine", {}))
    with pytest.raises(StoreError, match="zero vector"):
        store.query("rc_chk_0001", [0.0] * DIM, 3)
    with pytest.raises(StoreError, match="dimension"):
        store.query("rc_chk_0001", [1.0] * (DIM + 1), 3)
    assert store.query("rc_chk_0001", [1.0] * DIM, 0) == []
    assert store.query("rc_chk_0001", [1.0] * DIM, 5) == []  # empty collection
    with pytest.raises(CollectionNotFound):
        store.query("rc_chk_0099", [1.0] * DIM, 5)


def test_k_beyond_num_candidates_cap_runs_exact(opened: _Opened) -> None:
    store = opened.open()
    store.ensure_collection(CollectionSpec("rc_cap_0001", 3, "cosine", {}))
    rng = random.Random(8)
    recs = [
        Record(str(uuid.UUID(int=i + 1)), [rng.random() + 0.1 for _ in range(3)], {"document_id": "d"})
        for i in range(30)
    ]
    store.upsert("rc_cap_0001", recs)
    wait_visible(store, "rc_cap_0001")
    assert len(store.query("rc_cap_0001", [1.0, 0.5, 0.2], 10001)) == 30


def test_filter_exactness_of_approximate_search_on_a_larger_collection(opened: _Opened) -> None:
    """Recorded honestly: with a pre-filter the server returned min(k, matches) for rare and common values."""
    store = opened.open(exact=False)
    dim = 16
    store.ensure_collection(CollectionSpec("rc_big_filt", dim, "cosine", {"lang": "str"}))
    rng = random.Random(9)
    recs = [
        Record(
            str(uuid.UUID(int=i + 1)),
            random_vector(rng, dim),
            {"document_id": "d", "lang": "rare" if i % 400 == 0 else "common"},
        )
        for i in range(4000)
    ]
    for i in range(0, len(recs), 500):
        store.upsert("rc_big_filt", recs[i : i + 500])
    wait_visible(store, "rc_big_filt", timeout=180)
    for value, count in (("rare", 10), ("common", 3990)):
        f = parse_filter({"lang": value}, full_schema())
        for k in (5, 20):
            got = store.query("rc_big_filt", random_vector(rng, dim), k, f)
            assert len(got) == min(k, count), (value, k, len(got))


def test_missing_prefix_and_database_isolation(opened: _Opened) -> None:
    a, b = opened.open(prefix="a_"), opened.open(prefix="b_")
    a.ensure_collection(CollectionSpec("rc_iso_0001", DIM, "cosine", {}))
    assert b.collection_info("rc_iso_0001") is None
    assert a.collection_info("rc_iso_0001") is not None
    other = opened.open(database=opened.database + "_o")
    try:
        assert other.collection_info("rc_iso_0001") is None
    finally:
        other._client.drop_database(opened.database + "_o")
