"""Milvus adapter tests.

* Needs ``pymilvus`` (extra ``milvus``). The ``lite`` runs use a real Milvus Lite
  database in a temporary directory. The ``server`` runs (marked ``integration``) use a Milvus
  standalone server at ``RAGCENTRE_TEST_MILVUS_URI`` (default ``http://127.0.0.1:59530``, the compose
  service in ``integration/t-milvus.md``) and are skipped when it is not reachable. Every test uses
  its own collection prefix and removes its collections afterwards.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

pymilvus = pytest.importorskip("pymilvus")

from pydantic import ValidationError  # noqa: E402

from ragcentre.errors import CollectionMismatch, InvalidFilter  # noqa: E402
from ragcentre.models.filters import matches, parse_filter  # noqa: E402
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, Record, StoreError, VectorStore  # noqa: E402
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract  # noqa: E402

SCHEMA: dict[str, Any] = {"lang": "str", "year": "int", "score": "float", "public": "bool", "document_id": "str"}
SERVER_URI = os.environ.get("RAGCENTRE_TEST_MILVUS_URI", "http://127.0.0.1:59530")


# ---- live fixtures --------------------------------------------------------------------------------

from ragcentre.stores.milvus import (  # noqa: E402
    BEST_CASE,
    CATALOG_SUFFIX,
    MAX_TOPK,
    STORE_TYPE,
    MilvusConfig,
    MilvusStore,
    _factory,
    is_lite_uri,
)
from ragcentre.stores.registry import vector_store_types  # noqa: E402

TARGETS = ["lite", pytest.param("server", marks=pytest.mark.integration)]


def _server_problem() -> str | None:
    try:
        client = pymilvus.MilvusClient(uri=SERVER_URI, timeout=5)
    except Exception as exc:
        return str(exc).splitlines()[0]
    try:
        client.list_collections(timeout=5)
    except Exception as exc:
        return str(exc).splitlines()[0]
    finally:
        client.close()
    return None


@pytest.fixture(scope="session")
def lite_client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """One Milvus Lite database for the whole run (it holds an exclusive lock on its directory)."""
    client = pymilvus.MilvusClient(uri=str(tmp_path_factory.mktemp("milvus-lite") / "shared.db"))
    yield client
    client.close()


@pytest.fixture(scope="session")
def server_client() -> Iterator[Any]:
    problem = _server_problem()
    if problem is not None:
        pytest.skip(f"Milvus standalone is not reachable at {SERVER_URI} ({problem})")
    client = pymilvus.MilvusClient(uri=SERVER_URI)
    yield client
    client.close()


def _new_prefix() -> str:
    return f"t{uuid.uuid4().hex[:10]}_"


def _drop_prefixed(client: Any, prefix: str) -> None:
    for name in client.list_collections():
        if name.startswith(prefix):
            client.drop_collection(name)


class Env:
    """A target (Milvus Lite or a server) plus a fresh collection prefix, cleaned up afterwards."""

    def __init__(self, kind: str, client: Any, target: str) -> None:
        self.kind, self.client, self.target = kind, client, target
        self.prefix = _new_prefix()

    def store(self, **kw: Any) -> MilvusStore:
        kw.setdefault("prefix", self.prefix)
        return MilvusStore(self.client, target=self.target, **kw)

    def physical(self, name: str, prefix: str | None = None) -> str:
        return (self.prefix if prefix is None else prefix) + name

    def cleanup(self) -> None:
        _drop_prefixed(self.client, self.prefix)


@pytest.fixture(params=TARGETS)
def env(request: pytest.FixtureRequest) -> Iterator[Env]:
    if request.param == "lite":
        e = Env("lite", request.getfixturevalue("lite_client"), "lite")
    else:
        e = Env("server", request.getfixturevalue("server_client"), SERVER_URI)
    yield e
    e.cleanup()


# ---- the contract ---------------------------------------------------------------------------------


class TestMilvusContract(VectorStoreContract):
    @pytest.fixture
    def store(self, env: Env) -> VectorStore:
        return env.store()


class TestMilvusHnswContract(VectorStoreContract):
    """HNSW is approximate: filtered queries are held to recall, unfiltered ones stay exact here."""

    @pytest.fixture
    def store(self, env: Env) -> VectorStore:
        s = env.store(index_type="HNSW")
        assert not s.capabilities.filter_exact
        return s


class TestMilvusOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"index_type": "FLAT"}
    options_b: ClassVar[dict[str, Any]] = {"index_type": "HNSW", "hnsw_m": 8}

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> VectorStore:
        return MilvusStore.connect(uri=str(tmp_path / "options.db"), **options)


class TestMilvusServerOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"index_type": "HNSW", "hnsw_m": 16}
    options_b: ClassVar[dict[str, Any]] = {"index_type": "HNSW", "hnsw_m": 32}
    pytestmark: ClassVar[Any] = pytest.mark.integration

    @pytest.fixture(autouse=True)
    def _scope(self, server_client: Any, tmp_path: Path) -> Iterator[None]:
        self.prefix = "t" + hashlib.sha1(str(tmp_path).encode()).hexdigest()[:10] + "_"
        yield
        _drop_prefixed(server_client, self.prefix)

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> VectorStore:
        return MilvusStore.connect(uri=SERVER_URI, prefix=self.prefix, **options)


class TestMilvusHnswOptions(CollectionOptionsContract):
    """Changing the HNSW build options on a Lite database is refused the same way."""

    options_a: ClassVar[dict[str, Any]] = {"index_type": "HNSW", "hnsw_m": 16}
    options_b: ClassVar[dict[str, Any]] = {"index_type": "HNSW", "hnsw_ef_construction": 100}

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> VectorStore:
        return MilvusStore.connect(uri=str(tmp_path / "hnsw.db"), **options)


# ---- adapter behaviour on both targets ------------------------------------------------------------


def _spec(name: str = "rc_milvus_0001", dim: int = 4, metric: Any = "cosine", **fields: str) -> CollectionSpec:
    return CollectionSpec(name=name, dim=dim, metric=metric, fields=dict(fields) or {"lang": "str"})


def test_is_a_vector_store(env: Env) -> None:
    s = env.store()
    assert isinstance(s, VectorStore)
    assert s.capabilities.metrics == frozenset({"cosine", "l2", "dot"})
    assert s.capabilities.max_dim == 32768
    assert s.capabilities.filter_exact is True
    assert s.capabilities.consistency == "immediate"
    assert env.store(index_type="HNSW").capabilities.filter_exact is False
    s.healthcheck()


def test_missing_collection(env: Env) -> None:
    s = env.store()
    with pytest.raises(CollectionNotFound):
        s.query("rc_missing_0001", [1.0, 0.0], 3)
    with pytest.raises(CollectionNotFound):
        s.upsert("rc_missing_0001", [Record("a", [1.0, 0.0], {})])
    s.delete("rc_missing_0001", ["a"])  # no-op
    s.drop_collection("rc_missing_0001")  # idempotent
    assert s.collection_info("rc_missing_0001") is None


def test_scores_per_metric(env: Env) -> None:
    s = env.store()
    for metric in ("cosine", "dot", "l2"):
        name = f"rc_score_{metric}"
        s.ensure_collection(_spec(name, dim=2, metric=metric))
        s.upsert(name, [Record("p", [3.0, 4.0], {}), Record("q", [0.0, 1.0], {})])
        got = {m.id: m.score for m in s.query(name, [0.0, 0.0] if metric == "l2" else [1.0, 1.0], 2)}
        if metric == "l2":
            assert got["p"] == pytest.approx(-5.0, abs=1e-4)  # squared distance 25 -> -sqrt
            assert got["q"] == pytest.approx(-1.0, abs=1e-4)
        elif metric == "dot":
            assert got["p"] == pytest.approx(7.0, abs=1e-4) and got["q"] == pytest.approx(1.0, abs=1e-4)
        else:
            assert got["p"] == pytest.approx(7 / (5 * math.sqrt(2)), abs=1e-4)
            assert got["q"] == pytest.approx(1 / math.sqrt(2), abs=1e-4)


def test_count_and_query_see_writes_immediately(env: Env) -> None:
    s = env.store()
    s.ensure_collection(_spec(dim=2))
    for i in range(5):
        s.upsert("rc_milvus_0001", [Record(f"id{i}", [1.0, float(i)], {"lang": "en"})])
        info = s.collection_info("rc_milvus_0001")
        assert info is not None and info.count == i + 1
        assert f"id{i}" in {m.id for m in s.query("rc_milvus_0001", [1.0, float(i)], 10)}
    s.delete("rc_milvus_0001", ["id0", "id1"])
    info = s.collection_info("rc_milvus_0001")
    assert info is not None and info.count == 3
    assert not {"id0", "id1"} & {m.id for m in s.query("rc_milvus_0001", [1.0, 0.0], 10)}


def test_ids_with_special_characters(env: Env) -> None:
    s = env.store()
    s.ensure_collection(_spec(dim=2))
    ids = ['a"b', "a\\b", "x y", "new\nline", "é中\U0001f600", "' or 1=1", "a" * 128]
    s.upsert("rc_milvus_0001", [Record(i, [1.0, float(n)], {}) for n, i in enumerate(ids)])
    info = s.collection_info("rc_milvus_0001")
    assert info is not None and info.count == len(ids)
    assert {m.id for m in s.query("rc_milvus_0001", [1.0, 0.0], 20)} == set(ids)
    s.delete("rc_milvus_0001", [ids[0], ids[1]])
    info = s.collection_info("rc_milvus_0001")
    assert info is not None and info.count == len(ids) - 2


ESCAPE_CORPUS = [
    "plain", 'quo"te', "sing'le", "back\\slash", 'a\\"b', "new\nline", "tab\tx", "cr\rx", "ctl\x01x",
    "unicé中\U0001f600", '" || true || "', "\\", '"', "'", "a b", "% _ *", " sep", "\x7f", "end\\",
    'x"] || exists(metadata["lang"]) || ["', "  line sep",
]  # fmt: skip


def test_string_values_round_trip_through_the_filter_compiler(env: Env) -> None:
    """Each stored value is matched by its own literal, and by nothing else, on a real server."""
    s = env.store()
    s.ensure_collection(_spec(dim=2))
    s.upsert("rc_milvus_0001", [Record(f"r{i}", [1.0, 0.0], {"lang": v}) for i, v in enumerate(ESCAPE_CORPUS)])
    schema = {"lang": "str", "document_id": "str"}
    for i, v in enumerate(ESCAPE_CORPUS):
        f = parse_filter({"lang": v}, schema)
        assert [m.id for m in s.query("rc_milvus_0001", [1.0, 0.0], 50, f)] == [f"r{i}"], repr(v)
        others = parse_filter({"lang": {"$ne": v}}, schema)
        got = {m.id for m in s.query("rc_milvus_0001", [1.0, 0.0], 50, others)}
        assert got == {f"r{j}" for j in range(len(ESCAPE_CORPUS)) if j != i}, repr(v)


def test_filter_values_with_nul_are_refused(env: Env) -> None:
    s = env.store()
    s.ensure_collection(_spec(dim=2))
    s.upsert("rc_milvus_0001", [Record("a", [1.0, 0.0], {"lang": "en"})])
    with pytest.raises(InvalidFilter):
        s.query("rc_milvus_0001", [1.0, 0.0], 5, parse_filter({"lang": "a\x00b"}, SCHEMA))


def test_missing_and_mixed_fields_follow_the_reference_evaluator(env: Env) -> None:
    s = env.store()
    s.ensure_collection(_spec(dim=2, lang="str", year="int", score="float", public="bool"))
    rows = [
        {"lang": "en", "year": 2020, "score": 0.5, "public": True},
        {"lang": "de", "year": 2024, "score": 1.0, "public": False},
        {"lang": "en"},  # year, score, public missing
        {},
    ]
    s.upsert("rc_milvus_0001", [Record(f"r{i}", [1.0, float(i)], m) for i, m in enumerate(rows)])
    cases = [
        {"lang": {"$ne": "de"}}, {"year": {"$ne": 2020}}, {"year": {"$nin": [2020, 2024]}}, {"year": {"$gte": 2020}},
        {"score": 1}, {"score": {"$in": [1, 0.5]}}, {"public": {"$ne": True}}, {"lang": {"$in": ["en", "de"]}},
        {"$or": [{"year": {"$lt": 2022}}, {"lang": "de"}]}, {"$and": [{"lang": "en"}, {"year": {"$ne": 2020}}]},
    ]  # fmt: skip
    for raw in cases:
        f = parse_filter(raw, SCHEMA)
        want = {f"r{i}" for i, m in enumerate(rows) if matches(f, m)}
        got = {m.id for m in s.query("rc_milvus_0001", [1.0, 0.0], 10, f)}
        assert got == want, raw


def test_float_field_matches_integer_filter_values(env: Env) -> None:
    s = env.store()
    s.ensure_collection(_spec("rc_float_0001", dim=2, score="float"))
    s.upsert("rc_float_0001", [Record("a", [1.0, 0.0], {"score": 1}), Record("b", [0.0, 1.0], {"score": 0.5})])
    for raw, want in [
        ({"score": 1}, {"a"}),
        ({"score": {"$in": [1, 0.5]}}, {"a", "b"}),
        ({"score": {"$nin": [1]}}, {"b"}),
    ]:
        got = s.query("rc_float_0001", [1.0, 1.0], 5, parse_filter(raw, SCHEMA))
        assert {m.id for m in got} == want, raw


def test_ensure_collection_recovers_a_catalog_entry_without_a_collection(env: Env) -> None:
    s = env.store()
    s.ensure_collection(_spec(dim=2, metric="cosine"))
    env.client.drop_collection(env.physical("rc_milvus_0001"))  # someone dropped it behind our back
    s2 = env.store()
    assert s2.collection_info("rc_milvus_0001") is None
    s2.ensure_collection(_spec(dim=3, metric="l2"))  # a stale entry never blocks a fresh definition
    info = s2.collection_info("rc_milvus_0001")
    assert info is not None and (info.dim, info.metric) == (3, "l2")


def test_existing_collection_with_other_shape_is_a_mismatch_and_leaves_no_entry(env: Env) -> None:
    s = env.store()
    m = pymilvus
    physical = env.physical("rc_foreign_0001")
    schema = env.client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id", m.DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("vector", m.DataType.FLOAT_VECTOR, dim=5)
    schema.add_field("metadata", m.DataType.JSON)
    idx = env.client.prepare_index_params()
    idx.add_index("vector", index_type="FLAT", metric_type="L2")
    env.client.create_collection(physical, schema=schema, index_params=idx, consistency_level="Strong")
    with pytest.raises(CollectionMismatch):
        s.ensure_collection(_spec("rc_foreign_0001", dim=4, metric="l2"))
    with pytest.raises(CollectionMismatch):
        s.ensure_collection(_spec("rc_foreign_0001", dim=5, metric="cosine"))
    assert s.collection_info("rc_foreign_0001") is None  # no catalog entry was left behind
    s.ensure_collection(_spec("rc_foreign_0001", dim=5, metric="l2"))  # a matching shape is adopted
    assert s.collection_info("rc_foreign_0001") is not None


def test_dimension_and_input_validation(env: Env) -> None:
    s = env.store()
    with pytest.raises(StoreError, match="at least 2"):
        s.ensure_collection(_spec(dim=1))
    s.ensure_collection(_spec(dim=3))
    name = "rc_milvus_0001"
    with pytest.raises(StoreError, match="dimension"):
        s.upsert(name, [Record("a", [1.0, 0.0], {})])
    with pytest.raises(StoreError, match="dimension"):
        s.query(name, [1.0, 0.0], 3)
    with pytest.raises(StoreError, match="non-finite"):
        s.upsert(name, [Record("a", [1.0, float("nan"), 0.0], {})])
    with pytest.raises(StoreError, match="non-finite"):
        s.query(name, [1.0, float("inf"), 0.0], 3)
    with pytest.raises(StoreError, match=str(MAX_TOPK)):
        s.query(name, [1.0, 0.0, 0.0], MAX_TOPK + 1)
    assert s.query(name, [1.0, 0.0, 0.0], 0) == []
    with pytest.raises(StoreError, match="max_batch"):
        s.upsert(name, [Record(f"i{i}", [1.0, 0.0, 0.0], {}) for i in range(s.capabilities.max_batch + 1)])
    s.upsert(name, [])  # empty batch is fine
    s.delete(name, [])


def test_collection_names_and_prefix(env: Env) -> None:
    with pytest.raises(StoreError, match="prefix"):
        env.store(prefix="1bad")
    with pytest.raises(StoreError, match="prefix"):
        env.store(prefix="has-dash")
    with pytest.raises(StoreError, match="prefix"):
        env.store(prefix="x" * 200)
    s = env.store()
    for bad in ("Bad", "a", "has-dash", "x" * 64, CATALOG_SUFFIX):
        with pytest.raises(StoreError):
            s.ensure_collection(_spec(bad))
    s.ensure_collection(_spec("rc_milvus_0001"))
    assert env.physical("rc_milvus_0001") in env.client.list_collections()
    assert env.physical(CATALOG_SUFFIX) in env.client.list_collections()
    # a different prefix sees nothing
    other = env.store(prefix=_new_prefix())
    assert other.collection_info("rc_milvus_0001") is None
    other.ensure_collection(_spec("rc_milvus_0001", dim=6, metric="dot"))  # independent definition
    info = s.collection_info("rc_milvus_0001")
    assert info is not None and info.dim == 4
    _drop_prefixed(env.client, other._prefix)


def test_two_store_objects_share_one_catalog(env: Env) -> None:
    a, b = env.store(), env.store()
    a.ensure_collection(_spec(dim=3, metric="dot"))
    info = b.collection_info("rc_milvus_0001")
    assert info is not None and (info.dim, info.metric) == (3, "dot")
    with pytest.raises(CollectionMismatch):
        b.ensure_collection(_spec(dim=3, metric="cosine"))
    b.upsert("rc_milvus_0001", [Record("x", [1.0, 0.0, 0.0], {})])
    assert [m.id for m in a.query("rc_milvus_0001", [1.0, 0.0, 0.0], 1)] == ["x"]
    a.drop_collection("rc_milvus_0001")
    assert b.collection_info("rc_milvus_0001") is None
    with pytest.raises(CollectionNotFound):
        b.query("rc_milvus_0001", [1.0, 0.0, 0.0], 1)  # b's cached state is invalidated by the drop


def test_drop_and_recreate_invalidates_cached_state(env: Env) -> None:
    s = env.store()
    s.ensure_collection(_spec("rc_cache_0001", dim=2, metric="l2"))
    s.upsert("rc_cache_0001", [Record("a", [3.0, 4.0], {})])
    assert s.query("rc_cache_0001", [0.0, 0.0], 1)[0].score == pytest.approx(-5.0, abs=1e-4)
    s.drop_collection("rc_cache_0001")
    s.ensure_collection(_spec("rc_cache_0001", dim=2, metric="dot"))
    s.upsert("rc_cache_0001", [Record("a", [3.0, 4.0], {})])
    assert s.query("rc_cache_0001", [1.0, 1.0], 1)[0].score == pytest.approx(7.0, abs=1e-4)


def test_adding_fields_does_not_rebuild_the_collection(env: Env) -> None:
    s = env.store()
    s.ensure_collection(_spec(dim=2, lang="str"))
    s.upsert("rc_milvus_0001", [Record("a", [1.0, 0.0], {"lang": "en"})])
    s.ensure_collection(_spec(dim=2, lang="str", year="int"))
    s.upsert("rc_milvus_0001", [Record("b", [0.0, 1.0], {"lang": "en", "year": 2020})])
    got = s.query("rc_milvus_0001", [1.0, 1.0], 5, parse_filter({"year": {"$gte": 2020}}, SCHEMA))
    assert [m.id for m in got] == ["b"]
    info = s.collection_info("rc_milvus_0001")
    assert info is not None and info.count == 2  # the existing record survived
    with pytest.raises(CollectionMismatch):
        s.ensure_collection(_spec(dim=2, lang="int"))  # changing a field's type is refused


def test_hnsw_query_with_large_k(env: Env) -> None:
    """ef is raised to k, because Milvus refuses a search whose limit exceeds ef."""
    s = env.store(index_type="HNSW", hnsw_ef=16)
    s.ensure_collection(_spec(dim=4))
    rng = random.Random(3)
    s.upsert("rc_milvus_0001", [Record(f"r{i}", [rng.random() for _ in range(4)], {"lang": "en"}) for i in range(120)])
    assert len(s.query("rc_milvus_0001", [0.5] * 4, 100)) == 100


def test_hnsw_options_are_recorded_and_checked(env: Env) -> None:
    a = env.store(index_type="HNSW", hnsw_m=24)
    a.ensure_collection(_spec(dim=4))
    env.store(index_type="HNSW", hnsw_m=24, hnsw_ef=200).ensure_collection(_spec(dim=4))  # query-time: no mismatch
    for other in (env.store(index_type="HNSW", hnsw_m=32), env.store(index_type="FLAT")):
        with pytest.raises(CollectionMismatch):
            other.ensure_collection(_spec(dim=4))


def test_concurrent_ensure_is_safe(env: Env) -> None:
    import threading

    errors: list[BaseException] = []

    def run() -> None:
        try:
            env.store().ensure_collection(_spec("rc_race_0001", dim=4))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    info = env.store().collection_info("rc_race_0001")
    assert info is not None and info.count == 0


def test_a_failing_client_raises_store_error() -> None:
    class Broken:
        def __getattr__(self, name: str) -> Callable[..., Any]:
            def fail(*args: Any, **kwargs: Any) -> Any:
                raise ConnectionError("connection refused")

            return fail

    bad = MilvusStore(Broken(), target="broken", prefix=_new_prefix())
    with pytest.raises(StoreError, match="not reachable"):
        bad.healthcheck()
    with pytest.raises(StoreError, match="connection refused"):
        bad.collection_info("rc_milvus_0001")
    with pytest.raises(StoreError, match="connection refused"):
        bad.query("rc_milvus_0001", [1.0, 0.0], 1)


def test_query_path_calls_do_not_retry(env: Env) -> None:
    """Search, count and catalog reads pass retry_times=0 (section 20: no retries on queries)."""
    s = env.store()
    s.ensure_collection(_spec(dim=2))
    s.upsert("rc_milvus_0001", [Record("a", [1.0, 0.0], {})])
    seen: list[tuple[str, dict[str, Any]]] = []
    real = env.client

    class Spy:
        def __getattr__(self, name: str) -> Any:
            fn = getattr(real, name)

            def call(*args: Any, **kwargs: Any) -> Any:
                seen.append((name, kwargs))
                return fn(*args, **kwargs)

            return call

    spy = MilvusStore(Spy(), target=env.target, prefix=env.prefix)
    spy.query("rc_milvus_0001", [1.0, 0.0], 1)
    spy.collection_info("rc_milvus_0001")
    reads = [kw for name, kw in seen if name in ("search", "query", "has_collection")]
    assert reads and all(kw.get("retry_times") == 0 for kw in reads), seen
    assert all(kw.get("consistency_level") == "Strong" for name, kw in seen if name in ("search", "query"))


# ---- configuration, factory and registry ----------------------------------------------------------


def test_registry_type_shape() -> None:
    assert STORE_TYPE.type == "milvus"
    assert STORE_TYPE.secret_keys == ("token",)
    assert STORE_TYPE.immutable_fields == ("index_type", "hnsw_m", "hnsw_ef_construction")
    assert STORE_TYPE.capabilities == BEST_CASE
    assert STORE_TYPE.config_model is MilvusConfig


def test_loads_through_the_registry_entry_point_format() -> None:
    from ragcentre.stores import registry

    assert registry._load("ragcentre.stores.milvus:STORE_TYPE") is STORE_TYPE
    assert vector_store_types is not None


def test_config_validation() -> None:
    ok = MilvusConfig.model_validate({"uri": "./milvus.db", "prefix": "app_", "db_name": "rag"})
    assert ok.timeout == 30.0 and ok.index_type == "FLAT"
    MilvusConfig.model_validate({"uri": "https://in01-abc.api.gcp-us-west1.zillizcloud.com", "hnsw_m": 32})
    MilvusConfig.model_validate({"uri": "http://localhost:19530", "index_type": "HNSW"})
    for bad in (
        {},
        {"uri": ""},
        {"uri": "data.sqlite"},  # not a .db path and not a URL
        {"uri": "http://h", "prefix": "1x"},
        {"uri": "http://h", "prefix": "a-b"},
        {"uri": "http://h", "db_name": "1db"},
        {"uri": "http://h", "db_name": "a;b"},
        {"uri": "http://h", "index_type": "IVF_FLAT"},
        {"uri": "http://h", "hnsw_m": 1},
        {"uri": "http://h", "hnsw_ef": 0},
        {"uri": "http://h", "timeout": 0},
        {"uri": "http://h", "bogus": 1},
    ):
        with pytest.raises(ValidationError):
            MilvusConfig.model_validate(bad)
    assert is_lite_uri("data/m.db") and not is_lite_uri("http://h:19530") and not is_lite_uri("https://h")


def test_factory_builds_a_lite_store_inside_the_data_dir(tmp_path: Path) -> None:
    cfg = MilvusConfig.model_validate({"uri": "sub/dir/milvus.db", "prefix": "f_"})
    store = _factory(cfg, {}, tmp_path)
    try:
        store.healthcheck()
        store.ensure_collection(_spec(dim=2))
        store.upsert("rc_milvus_0001", [Record("a", [1.0, 0.0], {"lang": "en"})])
        assert [m.id for m in store.query("rc_milvus_0001", [1.0, 0.0], 1)] == ["a"]
    finally:
        store.close()
    assert (tmp_path / "sub" / "dir" / "milvus.db").exists()


def test_lite_data_survives_reopening(tmp_path: Path) -> None:
    uri = str(tmp_path / "keep.db")
    a = MilvusStore.connect(uri=uri)
    a.ensure_collection(_spec(dim=2, metric="dot"))
    a.upsert("rc_milvus_0001", [Record("a", [1.0, 0.0], {"lang": "en"})])
    a.close()
    b = MilvusStore.connect(uri=uri)
    try:
        info = b.collection_info("rc_milvus_0001")
        assert info is not None and (info.dim, info.metric, info.count) == (2, "dot", 1)
        assert [m.id for m in b.query("rc_milvus_0001", [1.0, 0.0], 1, parse_filter({"lang": "en"}, SCHEMA))] == ["a"]
    finally:
        b.close()


def test_connect_failure_is_a_store_error() -> None:
    with pytest.raises(StoreError, match="could not connect"):
        MilvusStore.connect(uri="http://127.0.0.1:1", timeout=1)


def test_connection_errors_do_not_leak_credentials_in_the_uri() -> None:
    with pytest.raises(StoreError) as info:
        MilvusStore.connect(uri="http://user:hunter2@127.0.0.1:1")
    assert "hunter2" not in str(info.value)


def test_factory_passes_the_token_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_connect(cls: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "store"

    monkeypatch.setattr(MilvusStore, "connect", classmethod(fake_connect))
    cfg = MilvusConfig.model_validate({"uri": "https://zilliz.example", "db_name": "rag", "prefix": "p_"})
    assert _factory(cfg, {"token": "user:pw"}, tmp_path) == "store"
    assert seen["token"] == "user:pw" and seen["uri"] == "https://zilliz.example"
    assert seen["db_name"] == "rag" and seen["prefix"] == "p_"
    _factory(cfg, {}, tmp_path)
    assert seen["token"] is None
