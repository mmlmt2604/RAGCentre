"""Redis vector store tests.

Pure unit tests (config, encoding, filter compiler, score transform, reply parsing, catalog) need no
server. The live tests need Redis 8 or Redis Stack (see integration/t-redis.md for the compose service):

    docker compose -p rc-t-redis -f <compose file> up -d
    RAGCENTRE_TEST_REDIS_URL=redis://127.0.0.1:56379/0 uv run pytest tests/test_vs_redis.py

They skip when the ``redis`` extra is missing or the server is unreachable. Each test uses its own
key prefix and removes its indexes, records and catalog keys afterwards.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import FieldType, matches, parse_filter
from ragcentre.stores.base import (
    CollectionNotFound,
    CollectionSpec,
    Match,
    Record,
    StoreError,
    StoreUnavailable,
    VectorStore,
)
from ragcentre.stores.redis import (
    CATALOG_KEY_PREFIX,
    SCORE_FIELD,
    STORE_TYPE,
    RedisConfig,
    RedisStore,
    compile_filter,
    encode_tag,
    parse_matches,
    to_score,
)
from ragcentre.testing.random_data import SCHEMA_FIELDS, full_schema, random_vector
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract, brute_score

URL = os.environ.get("RAGCENTRE_TEST_REDIS_URL", "redis://127.0.0.1:56379/0")
FIELDS: dict[str, FieldType] = {**SCHEMA_FIELDS, "document_id": "str"}
HOSTILE = [
    "",
    " ",
    " lead",
    "trail ",
    "a b",
    "a,b",
    "a|b",
    "{x}",
    "}{",
    "@lang",
    "-dash",
    "it's",
    'say "hi"',
    "back\\slash",
    "(p) [b] ~t !n *",
    "%d $x #h &a ^c +p =e <>;:.",
    "\x00z",
    "tab\tnewline\n",
    "ünï ☃ 日本語 🙂",
    "Plain",
    "plain",
    "_",
    "s",
    "s_20",
    "x" * 600,
    "x" * 5000,
]


# ---- unit tests: no server -----------------------------------------------------------------


def test_encode_tag_alphabet_and_shape() -> None:
    for value in HOSTILE:
        tag = encode_tag(value)
        assert re.fullmatch(r"[sh][A-Za-z0-9_]*", tag), (value, tag)
        assert tag  # never empty
    assert encode_tag("") == "s"
    assert encode_tag("en") == "sen"
    assert encode_tag("a b") == "sa_20b"
    assert encode_tag("a_b") == "sa_5fb"  # the escape character is escaped
    assert encode_tag("é") == "s_c3_a9"
    assert re.fullmatch(r"h[0-9a-f]{64}", encode_tag("x" * 600))
    assert len(encode_tag("x" * 512)) == 513 and encode_tag("x" * 512).startswith("s")


def test_encode_tag_is_one_to_one_and_case_sensitive() -> None:
    tags = [encode_tag(v) for v in HOSTILE]
    assert len(set(tags)) == len(HOSTILE)
    assert encode_tag("Plain") != encode_tag("plain")
    assert encode_tag("s_20") != encode_tag(" ")  # an escaped-looking value is not a space
    # distinct on a larger random sample too
    rng = random.Random(7)
    sample = {"".join(rng.choice("ab _-,|{}@\\\"'é") for _ in range(rng.randint(0, 6))) for _ in range(3000)}
    assert len({encode_tag(v) for v in sample}) == len(sample)


def _compile(raw: Any) -> str:
    f = parse_filter(raw, FIELDS)
    assert f is not None
    return compile_filter(f, FIELDS)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"lang": "en"}, "@lang:{sen}"),
        ({"lang": {"$ne": "en"}}, "-@lang:{sen}"),
        ({"lang": {"$in": ["en", "a b"]}}, "@lang:{sen|sa_20b}"),
        ({"lang": {"$nin": ["en", "fr"]}}, "-@lang:{sen|sfr}"),
        ({"public": True}, "@public:{true}"),
        ({"public": {"$ne": True}}, "-@public:{true}"),
        ({"public": {"$in": [True, False]}}, "@public:{true|false}"),
        ({"public": {"$nin": [False]}}, "-@public:{false}"),
        ({"year": 2020}, "@year:[2020 2020]"),
        ({"year": {"$ne": 2020}}, "-@year:[2020 2020]"),
        ({"year": {"$gt": 2020}}, "@year:[(2020 +inf]"),
        ({"year": {"$gte": 2020}}, "@year:[2020 +inf]"),
        ({"year": {"$lt": 2020}}, "@year:[-inf (2020]"),
        ({"year": {"$lte": 2020}}, "@year:[-inf 2020]"),
        ({"year": {"$in": [2019, 2021]}}, "(@year:[2019 2019])|(@year:[2021 2021])"),
        ({"year": {"$nin": [2019, 2021]}}, "(-@year:[2019 2019]) (-@year:[2021 2021])"),
        ({"score": 0.5}, "@score:[0.5 0.5]"),
        ({"score": {"$gte": 1}}, "@score:[1 +inf]"),  # an int is valid for a float field
        ({"score": {"$lt": -0.25}}, "@score:[-inf (-0.25]"),
        ({"score": {"$gt": 1e20}}, "@score:[(1e+20 +inf]"),
        ({"year": {"$gte": -5}}, "@year:[-5 +inf]"),
        ({"document_id": "doc-01"}, "@document_id:{sdoc_2d01}"),
    ],
)
def test_filter_compiler_every_operator(raw: Any, expected: str) -> None:
    assert _compile(raw) == expected


def test_filter_compiler_combinators() -> None:
    assert _compile({"$and": [{"lang": "en"}, {"year": {"$gte": 2020}}]}) == "(@lang:{sen}) (@year:[2020 +inf])"
    assert _compile({"$or": [{"lang": "en"}, {"public": True}]}) == "(@lang:{sen})|(@public:{true})"
    nested = _compile({"$and": [{"$or": [{"lang": "en"}, {"lang": {"$ne": "fr"}}]}, {"year": {"$lt": 2000}}]})
    assert nested == "((@lang:{sen})|(-@lang:{sfr})) (@year:[-inf (2000])"
    # an $and with two conditions on one field is two parts, never merged
    assert _compile({"year": {"$gte": 2000, "$lt": 2010}}) == "(@year:[2000 +inf]) (@year:[-inf (2010])"


@pytest.mark.parametrize("op", ["$eq", "$ne"])
@pytest.mark.parametrize("value", HOSTILE)
def test_filter_compiler_hostile_strings_cannot_add_syntax(op: str, value: str) -> None:
    expr = _compile({"lang": {op: value}})
    m = re.fullmatch(r"-?@lang:\{([^{}]*)\}", expr)
    assert m is not None, expr
    assert re.fullmatch(r"[sh][A-Za-z0-9_]*", m.group(1)), expr


def test_filter_compiler_hostile_lists_and_ids() -> None:
    expr = _compile({"lang": {"$in": HOSTILE[:20]}})
    m = re.fullmatch(r"@lang:\{([^{}]*)\}", expr)
    assert m is not None
    parts = m.group(1).split("|")
    assert len(parts) == 20 and all(re.fullmatch(r"[sh][A-Za-z0-9_]*", p) for p in parts)
    expr = _compile({"document_id": {"$nin": ["x}) | (@year:[0 +inf]", "a|b"]}})
    assert re.fullmatch(r"-@document_id:\{[A-Za-z0-9_|]+\}", expr), expr


def test_filter_compiler_rejects_unknown_fields() -> None:
    f = parse_filter({"lang": "en"}, FIELDS)
    assert f is not None
    with pytest.raises(StoreError, match="not a filterable field"):
        compile_filter(f, {"year": "int"})
    # document_id is always available
    g = parse_filter({"document_id": "d"}, FIELDS)
    assert g is not None
    assert compile_filter(g, {}) == "@document_id:{sd}"


def test_filter_compiler_output_is_well_formed_on_random_filters() -> None:
    """Random filters compile to balanced expressions with nothing injected. (Semantics are tested live.)"""
    from ragcentre.testing.random_data import random_filter

    rng = random.Random(11)
    for _ in range(200):
        f = parse_filter(random_filter(rng), full_schema())
        assert f is not None
        expr = compile_filter(f, FIELDS)
        grouping = re.sub(r"\[[^\]]*\]", "", expr)  # an exclusive range bound is a literal "("
        assert grouping.count("(") == grouping.count(")")
        assert not re.search(r"[\"'\\;$]", expr)


def test_to_score_matches_the_verified_server_scales() -> None:
    # COSINE returns 1 - cos, IP returns 1 - dot, L2 returns the squared distance (all read from Redis 8.10.3)
    assert to_score("cosine", 0.506217241287) == pytest.approx(0.4937827587)
    assert to_score("dot", -4.05) == pytest.approx(5.05)
    assert to_score("l2", 11.49) == pytest.approx(-math.sqrt(11.49))
    assert to_score("l2", 0.0) == 0.0
    assert to_score("l2", -1e-9) == 0.0  # float noise below zero


def _reply(rows: list[tuple[str, str]], warning: list[bytes] | None = None) -> dict[bytes, Any]:
    return {
        b"results": [{b"id": k.encode(), b"extra_attributes": {SCORE_FIELD.encode(): v.encode()}} for k, v in rows],
        b"total_results": len(rows),
        b"warning": warning or [],
    }


def test_parse_matches_converts_orders_and_strips_prefix() -> None:
    got = parse_matches(_reply([("p:a", "0.5"), ("p:b", "0.1"), ("p:c", "0.9")]), "p:", "cosine")
    assert got == [Match("b", pytest.approx(0.9)), Match("a", pytest.approx(0.5)), Match("c", pytest.approx(0.1))]  # type: ignore[list-item]
    assert parse_matches(_reply([]), "p:", "cosine") == []


def test_parse_matches_refuses_a_partial_result() -> None:
    with pytest.raises(StoreError, match="incomplete"):
        parse_matches(_reply([("p:a", "0.5")], [b"Timeout limit was reached"]), "p:", "cosine")
    with pytest.raises(StoreError, match="unexpected key"):
        parse_matches(_reply([("other:a", "0.5")]), "p:", "cosine")
    with pytest.raises(StoreError, match="RESP3"):
        parse_matches([1, b"p:a", [b"x", b"1"]], "p:", "cosine")


def test_config_defaults_and_validation() -> None:
    cfg = RedisConfig()
    assert (cfg.url, cfg.algorithm, cfg.ef_runtime, cfg.prefix) == ("redis://localhost:6379/0", "flat", None, "")
    assert RedisConfig(url="rediss://u:p@h:6380/1", algorithm="hnsw", ef_runtime=64, prefix="ragc:").ef_runtime == 64
    assert RedisConfig(url="unix:///var/run/redis.sock").url.startswith("unix://")
    for bad in (
        {"url": "http://localhost:6379"},
        {"url": "localhost:6379"},
        {"url": "redis://localhost?protocol=2"},
        {"algorithm": "ivf"},
        {"ef_runtime": 10},  # flat is the default algorithm
        {"algorithm": "hnsw", "ef_runtime": 0},
        {"prefix": "bad prefix"},
        {"prefix": "a*"},
        {"prefix": "a/b"},
        {"password": "x"},  # a secret, not config
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            RedisConfig(**bad)


def test_store_type_registration() -> None:
    assert STORE_TYPE.type == "redis"
    assert STORE_TYPE.secret_keys == ("password",)
    assert STORE_TYPE.immutable_fields == ("algorithm",)
    caps = STORE_TYPE.capabilities
    assert caps.metrics == frozenset({"cosine", "l2", "dot"})
    assert caps.consistency == "immediate" and caps.filter_exact is True and caps.max_dim is None
    assert set(STORE_TYPE.config_model.model_fields) == {"url", "algorithm", "ef_runtime", "prefix"}


def test_import_name_is_the_client_not_this_module() -> None:
    """``ragcentre.stores.redis`` must not shadow the third-party client for the adapter's own imports."""
    pytest.importorskip("redis")
    import redis

    import ragcentre.stores.redis as adapter

    assert redis is not adapter
    assert hasattr(redis, "Redis") and not hasattr(redis, "STORE_TYPE")
    assert adapter._import() is redis


# ---- unit tests: the client is needed, the server is not -------------------------------------


class FakeClient:
    """Just enough of the client for the catalog callbacks."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value.encode()

    def delete(self, *keys: str) -> None:
        for k in keys:
            self.data.pop(k, None)


@pytest.fixture
def fake() -> Iterator[tuple[FakeClient, Any]]:
    pytest.importorskip("redis")
    client = FakeClient()

    def make(**kw: Any) -> RedisStore:
        return RedisStore(client, prefix="pre_", **kw)  # type: ignore[arg-type]

    yield client, make


SPEC = CollectionSpec("rc_cat_0001", 8, "cosine", {"lang": "str", "year": "int"})


def test_catalog_round_trip(fake: Any) -> None:
    client, make = fake
    store = make(algorithm="flat")
    assert store._catalog.get(SPEC.name) is None
    entry = store._catalog.ensure(SPEC, store._options())
    key = CATALOG_KEY_PREFIX + "pre_rc_cat_0001"
    assert list(client.data) == [key]
    stored = json.loads(client.data[key])
    assert stored == {
        "dim": 8,
        "metric": "cosine",
        "fields": {"lang": "str", "year": "int"},
        "options": {"algorithm": "flat", "tag_encoding": 1},
    }
    assert store._catalog.get(SPEC.name) == entry
    # another store object on the same server reads the same entry
    assert make(algorithm="flat")._catalog.get(SPEC.name) == entry
    # new fields are added, none removed
    wider = CollectionSpec(SPEC.name, 8, "cosine", {"public": "bool"})
    merged = store._catalog.ensure(wider, store._options())
    assert merged.fields == {"lang": "str", "year": "int", "public": "bool"}
    store._catalog.drop(SPEC.name)
    store._catalog.drop(SPEC.name)  # idempotent
    assert client.data == {} and store._catalog.get(SPEC.name) is None


def test_catalog_mismatches(fake: Any) -> None:
    _, make = fake
    store = make(algorithm="flat")
    store._catalog.ensure(SPEC, store._options())
    with pytest.raises(CollectionMismatch):
        store._catalog.ensure(CollectionSpec(SPEC.name, 9, "cosine", {}), store._options())
    with pytest.raises(CollectionMismatch):
        store._catalog.ensure(CollectionSpec(SPEC.name, 8, "l2", {}), store._options())
    with pytest.raises(CollectionMismatch):
        store._catalog.ensure(CollectionSpec(SPEC.name, 8, "cosine", {"year": "float"}), store._options())
    with pytest.raises(CollectionMismatch, match="create a new store provider"):
        make(algorithm="hnsw")._catalog.ensure(SPEC, make(algorithm="hnsw")._options())
    # a different prefix is a different collection
    other = RedisStore(fake[0], prefix="other_")  # type: ignore[arg-type]
    other._catalog.ensure(CollectionSpec(SPEC.name, 4, "dot", {}), other._options())


def test_catalog_never_holds_connection_details(fake: Any) -> None:
    client, make = fake
    store = make(algorithm="hnsw", ef_runtime=64)
    store._catalog.ensure(SPEC, store._options())
    text = next(iter(client.data.values())).decode()
    assert "ef_runtime" not in text  # mutable query setting
    assert "redis://" not in text and "password" not in text


def test_connect_applies_password_secret_and_never_echoes_it() -> None:
    pytest.importorskip("redis")
    # Generate dummy credentials at runtime so secret scanners do not flag static URL passwords.
    url_password = uuid.uuid4().hex
    password = uuid.uuid4().hex
    url = f"redis://user:{url_password}@127.0.0.1:1/0"
    store = RedisStore.connect(url=url, password=password)
    try:
        kwargs = store._client.connection_pool.connection_kwargs
        assert kwargs["password"] == password and kwargs["username"] == "user"  # the secret wins
        assert kwargs["protocol"] == 3
        with pytest.raises(StoreError) as err:
            store.healthcheck()  # nothing listens on port 1
        assert password not in str(err.value) and url_password not in str(err.value)
    finally:
        store.close()
    with pytest.raises(StoreError) as bad:
        RedisStore.connect(url=f"redis://:{url_password}@127.0.0.1:1/0?protocol=2")
    assert url_password not in str(bad.value)
    with pytest.raises(StoreError) as bad_url:
        RedisStore.connect(url=f"notaredisurl://:{url_password}@host")
    assert url_password not in str(bad_url.value)


def test_authentication_failures_get_a_hint() -> None:
    from ragcentre.stores import redis as adapter

    class AuthenticationError(Exception):
        pass

    assert adapter._is_auth_failure(AuthenticationError("anything"))
    assert adapter._is_auth_failure(Exception("NOAUTH Authentication required."))
    assert adapter._is_auth_failure(Exception("HELLO must be called with the client already authenticated"))
    assert not adapter._is_auth_failure(Exception("Connection refused"))


def test_factory_builds_a_store_without_a_server(tmp_path: Path) -> None:
    pytest.importorskip("redis")
    cfg = RedisConfig(url="redis://127.0.0.1:1/0", algorithm="hnsw", ef_runtime=32, prefix="x_")
    store = STORE_TYPE.factory(cfg, {"password": "pw"}, tmp_path)
    try:
        assert isinstance(store, VectorStore)
        assert store._physical("rc_abc_0001") == "x_rc_abc_0001"  # type: ignore[attr-defined]
    finally:
        store.close()


def test_missing_extra_gives_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "redis", None)  # makes `import redis` raise ImportError
    with pytest.raises(StoreUnavailable, match=r"ragcentre\[redis\]"):
        RedisStore.connect(url="redis://127.0.0.1:1/0")


# ---- live tests ------------------------------------------------------------------------------


def _reachable() -> str | None:
    import redis

    try:
        client = redis.Redis.from_url(URL, protocol=3, socket_connect_timeout=3)
        client.ping()
        client.execute_command("FT._LIST")
        client.close()
    except redis.RedisError as exc:
        return str(exc).strip().splitlines()[0]
    return None


@pytest.fixture(scope="module")
def server() -> str:
    pytest.importorskip("redis")
    problem = _reachable()
    if problem is not None:
        pytest.skip(f"Redis with the search module is not reachable at the test URL ({problem})")
    return URL


def _wipe(prefix: str) -> None:
    """Remove every index, record, catalog entry and lock whose name starts with ``prefix``."""
    import redis

    client = redis.Redis.from_url(URL, protocol=3)
    try:
        for raw in client.execute_command("FT._LIST"):
            name = raw.decode() if isinstance(raw, bytes) else str(raw)
            if name.startswith(prefix):
                client.execute_command("FT.DROPINDEX", name, "DD")
        for pattern in (f"{prefix}*", f"ragcentre:collection:{prefix}*", f"ragcentre:lock:{prefix}*"):
            keys = list(client.scan_iter(match=pattern, count=1000))
            if keys:
                client.delete(*keys)
    finally:
        client.close()


@pytest.fixture
def fresh(server: str) -> Iterator[Any]:
    created: list[tuple[RedisStore, str]] = []

    def make(**kwargs: Any) -> RedisStore:
        prefix = kwargs.pop("prefix", None) or f"rct{uuid.uuid4().hex[:12]}_"
        s = RedisStore.connect(url=server, prefix=prefix, **kwargs)
        created.append((s, prefix))
        return s

    yield make
    for s, prefix in created:
        s.close()
        _wipe(prefix)


class TestRedisFlatContract(VectorStoreContract):
    @pytest.fixture
    def store(self, fresh: Any) -> RedisStore:
        return fresh(algorithm="flat")  # type: ignore[no-any-return]


class TestRedisHnswContract(VectorStoreContract):
    """HNSW is approximate in general; on the contract's 200 records it must still be exact."""

    @pytest.fixture
    def store(self, fresh: Any) -> RedisStore:
        return fresh(algorithm="hnsw", ef_runtime=200)  # type: ignore[no-any-return]


class TestRedisHnswDefaultEfContract(VectorStoreContract):
    @pytest.fixture
    def store(self, fresh: Any) -> RedisStore:
        return fresh(algorithm="hnsw")  # type: ignore[no-any-return]


class TestRedisOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"algorithm": "flat"}
    options_b: ClassVar[dict[str, Any]] = {"algorithm": "hnsw"}

    @pytest.fixture(autouse=True)
    def _isolated(self, server: str) -> Iterator[None]:
        self.prefix = f"rct{uuid.uuid4().hex[:12]}_"
        yield
        _wipe(self.prefix)

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> VectorStore:
        return RedisStore.connect(url=URL, prefix=self.prefix, **options)


def test_is_a_vector_store(fresh: Any) -> None:
    s = fresh()
    assert isinstance(s, VectorStore)
    assert s.capabilities.max_batch == 256 and s.capabilities.max_dim is None


def test_healthcheck_wrong_port_does_not_hang() -> None:
    pytest.importorskip("redis")
    s = RedisStore.connect(url="redis://127.0.0.1:1/0")
    try:
        with pytest.raises(StoreError, match="not reachable"):
            s.healthcheck()
    finally:
        s.close()


def test_missing_collection(fresh: Any) -> None:
    s = fresh()
    with pytest.raises(CollectionNotFound):
        s.query("rc_missing_0001", [1.0, 0.0], 3)
    with pytest.raises(CollectionNotFound):
        s.upsert("rc_missing_0001", [Record(str(uuid.uuid4()), [1.0, 0.0], {})])
    s.delete("rc_missing_0001", [str(uuid.uuid4())])
    s.drop_collection("rc_missing_0001")
    assert s.collection_info("rc_missing_0001") is None


def test_layout_in_redis(fresh: Any) -> None:
    """One index per collection over ``<prefix><collection>:<id>`` hashes, with a catalog key."""
    import redis

    s = fresh(algorithm="hnsw", prefix="rctlayout_")
    spec = CollectionSpec(
        "rc_layout_0001", 4, "dot", {"lang": "str", "year": "int", "public": "bool", "score": "float"}
    )
    s.ensure_collection(spec)
    rec = Record(
        str(uuid.uuid4()), [1.0, 2.0, 3.0, 4.0],
        {"document_id": "d1", "lang": "en", "year": 2021, "public": True, "score": 0.25},
    )  # fmt: skip
    s.upsert(spec.name, [rec])
    c = redis.Redis.from_url(URL, protocol=3)
    try:
        h = c.hgetall(f"rctlayout_rc_layout_0001:{rec.id}")
        got = {k.decode(): v for k, v in h.items()}
        assert got["lang"] == b"sen" and got["year"] == b"2021" and got["public"] == b"true"
        assert got["score"] == b"0.25" and got["document_id"] == b"sd1"
        assert len(got["__rc_vector"]) == 16
        info = c.execute_command("FT.INFO", "rctlayout_rc_layout_0001")
        assert info[b"index_definition"][b"prefixes"] == [b"rctlayout_rc_layout_0001:"]
        types = {a[b"identifier"]: a[b"type"] for a in info[b"attributes"]}
        assert types == {
            b"__rc_vector": b"VECTOR", b"document_id": b"TAG", b"lang": b"TAG", b"year": b"NUMERIC",
            b"public": b"TAG", b"score": b"NUMERIC",
        }  # fmt: skip
        vec = next(a for a in info[b"attributes"] if a[b"identifier"] == b"__rc_vector")
        assert (vec[b"algorithm"], vec[b"distance_metric"], vec[b"dim"]) == (b"HNSW", b"IP", 4)
        assert c.exists("ragcentre:collection:rctlayout_rc_layout_0001") == 1
    finally:
        c.close()
    s.drop_collection(spec.name)
    c = redis.Redis.from_url(URL, protocol=3)
    try:
        assert c.exists(f"rctlayout_rc_layout_0001:{rec.id}") == 0  # records go with the index
        assert c.exists("ragcentre:collection:rctlayout_rc_layout_0001") == 0
    finally:
        c.close()


def test_score_scales_are_exact_on_a_known_triangle(fresh: Any) -> None:
    """3-4-5: guards the verified server scales (L2 is squared; COSINE is 1-cos; IP is 1-dot)."""
    s = fresh()
    for metric, expected in (("l2", -5.0), ("dot", 0.0), ("cosine", 0.0)):
        name = f"rc_scale_{metric}_01"
        s.ensure_collection(CollectionSpec(name, 2, metric, {}))  # type: ignore[arg-type]
        s.upsert(name, [Record(str(uuid.uuid4()), [3.0, 4.0], {})])
        (m,) = s.query(name, [0.0, 0.0] if metric == "l2" else [4.0, -3.0], 1)
        assert m.score == pytest.approx(expected, abs=1e-5)
    s.ensure_collection(CollectionSpec("rc_scale_dot_02", 2, "dot", {}))
    s.upsert("rc_scale_dot_02", [Record(str(uuid.uuid4()), [3.0, 4.0], {})])
    assert s.query("rc_scale_dot_02", [2.0, 5.0], 1)[0].score == pytest.approx(26.0)  # raw dot product, not unit length


def test_hostile_string_values_round_trip_exactly(fresh: Any) -> None:
    s = fresh()
    spec = CollectionSpec("rc_hostile_0001", 4, "cosine", {"lang": "str"})
    s.ensure_collection(spec)
    rng = random.Random(3)
    records = [
        Record(str(uuid.uuid4()), random_vector(rng, 4), {"document_id": f"d{i}", "lang": v})
        for i, v in enumerate(HOSTILE)
    ]
    s.upsert(spec.name, records)
    schema: dict[str, FieldType] = {"lang": "str", "document_id": "str"}
    q = random_vector(rng, 4)
    for value in HOSTILE:
        for raw in (
            {"lang": value},
            {"lang": {"$ne": value}},
            {"lang": {"$in": [value, "zz"]}},
            {"lang": {"$nin": [value]}},
        ):
            f = parse_filter(raw, schema)
            got = {m.id for m in s.query(spec.name, q, 100, f)}
            want = {r.id for r in records if matches(f, r.metadata)}
            assert got == want, (raw, len(got), len(want))
    f = parse_filter({"$or": [{"lang": "x}) | (@lang:{y"}, {"document_id": {"$in": ["d0", "d1"]}}]}, schema)
    assert {m.id for m in s.query(spec.name, q, 100, f)} == {records[0].id, records[1].id}


def test_numeric_edges(fresh: Any) -> None:
    s = fresh()
    spec = CollectionSpec("rc_numeric_0001", 2, "l2", {"year": "int", "score": "float"})
    s.ensure_collection(spec)
    values = [(-2_000_000_000_000, -0.35), (-1, 0.0), (0, 1e-5), (2020, 0.1 + 0.2), (2**40, 1e20), (7, -1e-7)]
    records = [
        Record(str(uuid.uuid4()), [float(i), 0.0], {"document_id": "d", "year": y, "score": sc})
        for i, (y, sc) in enumerate(values)
    ]
    s.upsert(spec.name, records)
    schema: dict[str, FieldType] = {"year": "int", "score": "float", "document_id": "str"}
    for raw in (
        {"year": 2020}, {"year": {"$lt": 0}}, {"year": {"$gte": -1}}, {"year": {"$ne": 7}}, {"year": {"$nin": [0, 7, 2020]}},
        {"score": 0.1 + 0.2}, {"score": {"$gt": 1e-6}}, {"score": {"$lte": -0.35}}, {"score": {"$in": [1e20, 1e-5]}},
        {"score": {"$gt": 1}}, {"year": {"$gt": 2**39}},
    ):  # fmt: skip
        f = parse_filter(raw, schema)
        got = {m.id for m in s.query(spec.name, [0.0, 0.0], 100, f)}
        assert got == {r.id for r in records if matches(f, r.metadata)}, raw


def test_long_values_and_long_ids(fresh: Any) -> None:
    s = fresh()
    spec = CollectionSpec("rc_long_0001", 2, "cosine", {"source": "str"})
    s.ensure_collection(spec)
    longs = ["u" * n for n in (500, 513, 2000, 4097, 20000)]
    records = [
        Record(str(uuid.uuid4()), [1.0, float(i)], {"document_id": "d", "source": v}) for i, v in enumerate(longs)
    ]
    s.upsert(spec.name, records)
    for rec in records:
        f = parse_filter({"source": rec.metadata["source"]}, {"source": "str", "document_id": "str"})
        assert [m.id for m in s.query(spec.name, [1.0, 1.0], 10, f)] == [rec.id]


def test_large_embeddings(fresh: Any) -> None:
    """No dimension limit was found on the server (checked to 1,000,000 at index creation)."""
    s = fresh()
    spec = CollectionSpec("rc_big_0001", 3072, "cosine", {"lang": "str"})
    s.ensure_collection(spec)
    rng = random.Random(5)
    rec = Record(str(uuid.uuid4()), random_vector(rng, 3072), {"document_id": "d", "lang": "en"})
    other = Record(str(uuid.uuid4()), random_vector(rng, 3072), {"document_id": "d", "lang": "fr"})
    s.upsert(spec.name, [rec, other])
    top = s.query(spec.name, rec.vector, 2)
    assert top[0].id == rec.id and top[0].score == pytest.approx(
        brute_score("cosine", rec.vector, rec.vector), abs=1e-4
    )
    f = parse_filter({"lang": "fr"}, {"lang": "str"})
    assert [m.id for m in s.query(spec.name, rec.vector, 2, f)] == [other.id]
    s.ensure_collection(CollectionSpec("rc_big_0002", 65536, "l2", {}))
    info = s.collection_info("rc_big_0002")
    assert info is not None and info.dim == 65536


def test_adding_a_field_reindexes_existing_records(fresh: Any) -> None:
    """FT.ALTER indexes existing hashes in the background; ensure_collection waits for it."""
    s = fresh()
    s.ensure_collection(CollectionSpec("rc_alter_0001", 4, "cosine", {"lang": "str"}))
    rng = random.Random(9)
    records = [Record(str(uuid.uuid4()), random_vector(rng, 4), {"document_id": "d", "lang": "en"}) for _ in range(300)]
    for start in range(0, 300, 100):
        s.upsert("rc_alter_0001", records[start : start + 100])
    import redis

    c = redis.Redis.from_url(URL, protocol=3)
    try:  # simulate records written before the field existed carrying a value for it
        for i, rec in enumerate(records):
            c.hset(f"{s._prefix}rc_alter_0001:{rec.id}", "year", str(2000 + i % 2))
    finally:
        c.close()
    s.ensure_collection(CollectionSpec("rc_alter_0001", 4, "cosine", {"lang": "str", "year": "int"}))
    f = parse_filter({"year": 2001}, {"year": "int"})
    assert len(s.query("rc_alter_0001", [1.0, 0.0, 0.0, 0.0], 300, f)) == 150
    with pytest.raises(CollectionMismatch):
        s.ensure_collection(CollectionSpec("rc_alter_0001", 4, "cosine", {"year": "float"}))


def test_recovers_from_a_half_created_collection(fresh: Any) -> None:
    """A catalog entry without its index (a crash between the two, or an index dropped by hand)."""
    import redis

    s = fresh()
    s.ensure_collection(CollectionSpec("rc_stale_0001", 4, "cosine", {"lang": "str"}))
    s.upsert("rc_stale_0001", [Record(str(uuid.uuid4()), [1.0, 0.0, 0.0, 0.0], {"document_id": "d", "lang": "en"})])
    c = redis.Redis.from_url(URL, protocol=3)
    try:
        c.execute_command("FT.DROPINDEX", f"{s._prefix}rc_stale_0001")  # records stay, the index is gone
    finally:
        c.close()
    assert s.collection_info("rc_stale_0001") is None
    s.ensure_collection(CollectionSpec("rc_stale_0001", 6, "l2", {"year": "int"}))  # a new shape is fine now
    info = s.collection_info("rc_stale_0001")
    assert info is not None and (info.dim, info.metric, info.count) == (6, "l2", 0)  # the old records are gone


def test_adopts_an_index_whose_catalog_was_lost(fresh: Any) -> None:
    import redis

    s = fresh(algorithm="flat")
    spec = CollectionSpec("rc_adopt_0001", 4, "cosine", {"lang": "str"})
    s.ensure_collection(spec)
    c = redis.Redis.from_url(URL, protocol=3)
    try:
        c.delete(f"ragcentre:collection:{s._prefix}rc_adopt_0001")
    finally:
        c.close()
    s.ensure_collection(spec)  # the shape matches the index, so the catalog is rebuilt from the spec
    with pytest.raises(CollectionMismatch):
        s.ensure_collection(CollectionSpec("rc_adopt_0001", 5, "cosine", {}))
    other = RedisStore.connect(url=URL, prefix=s._prefix, algorithm="hnsw")
    try:
        c = redis.Redis.from_url(URL, protocol=3)
        try:
            c.delete(f"ragcentre:collection:{s._prefix}rc_adopt_0001")
        finally:
            c.close()
        with pytest.raises(CollectionMismatch, match="algorithm"):
            other.ensure_collection(spec)
    finally:
        other.close()


def test_concurrent_ensure_from_separate_stores(fresh: Any) -> None:
    """Two workers (separate clients, as separate processes would be) creating one collection."""
    import threading

    a = fresh(algorithm="hnsw", prefix="rctconc_")
    b = fresh(algorithm="hnsw", prefix="rctconc_")
    errors: list[BaseException] = []

    def go(store: RedisStore, fields: dict[str, FieldType]) -> None:
        try:
            for _ in range(5):
                store.ensure_collection(CollectionSpec("rc_conc_0001", 4, "cosine", fields))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=go, args=(a, {"lang": "str"})),
        threading.Thread(target=go, args=(b, {"year": "int"})),
        threading.Thread(target=go, args=(a, {"public": "bool"})),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    entry = a._catalog.get("rc_conc_0001")
    assert entry is not None and entry.fields == {"lang": "str", "year": "int", "public": "bool"}
    s = a.collection_info("rc_conc_0001")
    assert s is not None and s.dim == 4


def test_prefixes_isolate_collections(fresh: Any) -> None:
    a = fresh(prefix="rctiso_a_")
    b = fresh(prefix="rctiso_b_")
    spec = CollectionSpec("rc_iso_0001", 2, "cosine", {})
    a.ensure_collection(spec)
    b.ensure_collection(CollectionSpec("rc_iso_0001", 3, "l2", {}))  # same logical name, another shape
    a.upsert(spec.name, [Record(str(uuid.uuid4()), [1.0, 0.0], {"document_id": "d"})])
    ai, bi = a.collection_info(spec.name), b.collection_info(spec.name)
    assert ai is not None and bi is not None
    assert (ai.dim, ai.count) == (2, 1) and (bi.dim, bi.count) == (3, 0)
    a.drop_collection(spec.name)
    assert a.collection_info(spec.name) is None and b.collection_info(spec.name) is not None


def test_query_dimension_and_nonpositive_k(fresh: Any) -> None:
    s = fresh()
    s.ensure_collection(CollectionSpec("rc_dims_0001", 4, "cosine", {}))
    s.upsert("rc_dims_0001", [Record(str(uuid.uuid4()), [1.0, 0.0, 0.0, 0.0], {"document_id": "d"})])
    with pytest.raises(StoreError, match="dimension"):
        s.query("rc_dims_0001", [1.0, 0.0], 3)
    with pytest.raises(StoreError, match="dimension"):
        s.upsert("rc_dims_0001", [Record(str(uuid.uuid4()), [1.0], {"document_id": "d"})])
    assert s.query("rc_dims_0001", [1.0, 0.0, 0.0, 0.0], 0) == []
    assert s.query("rc_dims_0001", [1.0, 0.0, 0.0, 0.0], -3) == []
    with pytest.raises(StoreError, match="max_batch"):
        s.upsert("rc_dims_0001", [Record(str(uuid.uuid4()), [1.0, 0.0, 0.0, 0.0], {})] * 257)
    with pytest.raises(StoreError, match="must be int"):
        s.ensure_collection(CollectionSpec("rc_dims_0001", 4, "cosine", {"year": "int"}))
        s.upsert("rc_dims_0001", [Record(str(uuid.uuid4()), [1.0, 0.0, 0.0, 0.0], {"year": "2020"})])


def test_reserved_and_invalid_field_names(fresh: Any) -> None:
    s = fresh()
    for bad in ("__rc_vector", "__rc_x", "with space", "a-b", "1abc", "a:b"):
        with pytest.raises(StoreError, match="cannot be a Redis search field"):
            s.ensure_collection(CollectionSpec("rc_names_0001", 2, "cosine", {bad: "str"}))
    assert s.collection_info("rc_names_0001") is None
