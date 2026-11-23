"""LanceDB store: the contract suite on a local directory, plus config, catalog, ANN threshold and
filter-compiler tests. The compiler's string-level tests need no client; everything that touches a
table skips cleanly when the ``lancedb`` extra is not installed."""

from __future__ import annotations

import importlib.util
import math
import random
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import And, Cond, Or, matches, parse_filter
from ragcentre.stores.base import (
    CollectionNotFound,
    CollectionSpec,
    Record,
    StoreError,
    StoreUnavailable,
    VectorStore,
)
from ragcentre.stores.filter_datafusion import compile_datafusion, literal, quote_ident
from ragcentre.stores.lancedb import (
    INDEX_NAME,
    META_KEY,
    STORE_TYPE,
    LanceDBConfig,
    LanceDBStore,
    score_from_distance,
)
from ragcentre.testing.random_data import SCHEMA_FIELDS, full_schema, random_filter
from ragcentre.testing.vector_store_contract import CollectionOptionsContract, VectorStoreContract

HAVE_LANCEDB = importlib.util.find_spec("lancedb") is not None
needs_lancedb = pytest.mark.skipif(not HAVE_LANCEDB, reason="the 'lancedb' extra is not installed")

DIM = 8
NAME = "rc_lance_0001"


# ---- contract suite -------------------------------------------------------------------------


@needs_lancedb
class TestLanceDBContract(VectorStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[LanceDBStore]:
        s = LanceDBStore.connect(str(tmp_path / "lance"))
        yield s
        s.close()


@needs_lancedb
class TestLanceDBIndexTypeOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"index_type": "IVF_FLAT"}
    options_b: ClassVar[dict[str, Any]] = {"index_type": "IVF_PQ"}

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> LanceDBStore:
        return LanceDBStore.connect(str(tmp_path / "lance"), **options)


@needs_lancedb
class TestLanceDBThresholdOptions(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"ann_index_min_rows": 1000}
    options_b: ClassVar[dict[str, Any]] = {"ann_index_min_rows": None}

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> LanceDBStore:
        return LanceDBStore.connect(str(tmp_path / "lance"), **options)


# ---- helpers --------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LanceDBStore]:
    s = LanceDBStore.connect(str(tmp_path / "lance"))
    yield s
    s.close()


def records(n: int, *, dim: int = DIM, seed: int = 7, **meta: Any) -> list[Record]:
    rng = random.Random(seed)
    return [
        Record(
            str(uuid.UUID(int=rng.getrandbits(128), version=4)),
            [rng.uniform(-1, 1) for _ in range(dim)],
            {"document_id": f"doc-{i % 10}", "lang": "en", "year": 2020, "score": 0.5, "public": True, **meta},
        )
        for i in range(n)
    ]


def spec(
    name: str = NAME, dim: int = DIM, metric: str = "cosine", fields: dict[str, Any] | None = None
) -> CollectionSpec:
    return CollectionSpec(name, dim, metric, dict(SCHEMA_FIELDS) if fields is None else fields)  # type: ignore[arg-type]


def has_vector_index(store: LanceDBStore, name: str = NAME) -> bool:
    table, _ = store._require(name)
    return any(ix.name == INDEX_NAME for ix in table.list_indices())


# ---- filter compiler: string level ----------------------------------------------------------


def c(field: str, op: str, value: Any) -> Cond:
    return Cond(field, op, value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cond", "sql"),
    [
        (c("lang", "$eq", "en"), "`lang` = 'en'"),
        (c("lang", "$ne", "en"), "(`lang` IS NULL OR `lang` <> 'en')"),
        (c("year", "$gt", 2020), "`year` > 2020"),
        (c("year", "$gte", 2020), "`year` >= 2020"),
        (c("score", "$lt", 0.5), "`score` < 0.5"),
        (c("score", "$lte", 1), "`score` <= 1"),
        (c("lang", "$in", ("en", "fr")), "`lang` IN ('en', 'fr')"),
        (c("lang", "$nin", ("en", "fr")), "(`lang` IS NULL OR `lang` NOT IN ('en', 'fr'))"),
        (c("public", "$eq", True), "`public` = true"),
        (c("public", "$ne", False), "(`public` IS NULL OR `public` <> false)"),
        (c("year", "$in", (1, -2)), "`year` IN (1, -2)"),
    ],
)
def test_every_operator_compiles(cond: Cond, sql: str) -> None:
    assert compile_datafusion(cond) == sql


def test_combinators_nest_with_parentheses() -> None:
    f = And((c("a", "$eq", 1), Or((c("b", "$eq", "x"), c("c", "$lt", 2))), c("d", "$in", (True,))))
    assert compile_datafusion(f) == "(`a` = 1) AND ((`b` = 'x') OR (`c` < 2)) AND (`d` IN (true))"


def test_no_filter_is_true() -> None:
    assert compile_datafusion(None) == "true"


HOSTILE_STRINGS = [
    "it's",
    "'; DROP TABLE x; --",
    "' OR '1'='1",
    "a\\b",
    "\\'",
    "''",
    "x\n;",
    "\x00",
    "`; --",
    "/* */",
    "%_",
    "é😀",
]


@pytest.mark.parametrize("value", HOSTILE_STRINGS)
def test_hostile_string_values_stay_inside_one_literal(value: str) -> None:
    sql = compile_datafusion(c("lang", "$eq", value))
    assert sql.startswith("`lang` = '") and sql.endswith("'")
    inner = sql[len("`lang` = '") : -1]
    # Inside the literal every quote is doubled; removing the doubled pairs leaves none.
    assert "'" not in inner.replace("''", "")
    assert inner == value.replace("'", "''")


def test_lone_surrogates_are_rejected() -> None:
    with pytest.raises(ValueError, match="Unicode"):
        compile_datafusion(c("lang", "$eq", "bad\ud800"))


@pytest.mark.parametrize(
    "name",
    ["", "a b", "a;b", "a`b", "a'b", '"a"', "1a", "a-b", "a.b", "a b; DROP TABLE t", "x" * 65, "é", "a\n"],
)
def test_hostile_field_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="invalid field name"):
        compile_datafusion(c(name, "$eq", 1))
    with pytest.raises(ValueError, match="invalid field name"):
        quote_ident(name)


def test_allow_list_of_fields() -> None:
    assert compile_datafusion(c("lang", "$eq", "en"), allowed={"lang"}) == "`lang` = 'en'"
    with pytest.raises(ValueError, match="unknown field"):
        compile_datafusion(c("year", "$eq", 1), allowed={"lang"})
    with pytest.raises(ValueError, match="unknown field"):
        compile_datafusion(And((c("lang", "$eq", "en"), c("order", "$eq", "x"))), allowed={"lang"})


def test_literals() -> None:
    assert literal(True) == "true" and literal(False) == "false"
    assert literal(3) == "3" and literal(-3) == "-3"
    assert literal(2**63 - 1) == str(2**63 - 1) and literal(-(2**63)) == str(-(2**63))
    assert literal(0.1) == "0.1" and literal(1e22) == "1e+22" and literal(-0.0) == "-0.0"
    for bad in (2**63, -(2**63) - 1, math.nan, math.inf, -math.inf, None, b"x", [1]):
        with pytest.raises(ValueError):
            literal(bad)


# ---- filter compiler: against the engine ----------------------------------------------------


@needs_lancedb
class TestCompilerAgainstEngine:
    """The compiled text, run by DataFusion over rows that include NULLs and hostile strings, selects
    exactly the rows the reference evaluator ``matches`` selects."""

    ROWS: ClassVar[list[dict[str, Any]]] = [
        {"i": i, "document_id": f"doc-{i % 4}", "lang": lang, "year": year, "score": score, "public": public}
        for i, (lang, year, score, public) in enumerate(
            [
                ("en", 2020, 0.5, True), ("fr", 2018, 1.0, False), ("it's", 2022, 0.0, True),
                ("'; DROP TABLE t; --", 2019, 0.25, False), ("a\\b", None, None, None), (None, 2021, 0.75, None),
                ("zz", 2015, 1e-5, True), ("x\n;", 2025, 1e22, False), ("\x00z", 2020, -0.0, True),
                ("%_", 2020, 0.5, False),
            ]
        )
    ]  # fmt: skip

    @pytest.fixture
    def table(self, tmp_path: Path) -> Any:
        import lancedb
        import pyarrow as pa

        schema = pa.schema(
            [
                ("i", pa.int64()), ("document_id", pa.string()), ("lang", pa.string()), ("year", pa.int64()),
                ("score", pa.float64()), ("public", pa.bool_()),
            ]
        )  # fmt: skip
        data = pa.table({k: [r[k] for r in self.ROWS] for k in schema.names}, schema=schema)
        return lancedb.connect(str(tmp_path / "db")).create_table("t", data=data)

    def selected(self, table: Any, sql: str) -> set[int]:
        return set(table.search().where(sql).select(["i"]).limit(1000).to_arrow().column("i").to_pylist())

    def expected(self, f: Any) -> set[int]:
        return {r["i"] for r in self.ROWS if matches(f, {k: v for k, v in r.items() if v is not None})}

    @pytest.mark.parametrize("op", ["$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin"])
    def test_every_operator(self, table: Any, op: str) -> None:
        samples: dict[str, list[Any]] = {
            "lang": ["en", "it's", "'; DROP TABLE t; --", "a\\b", "x\n;", "\x00z", "%_"],
            "year": [2020, 2019],
            "score": [0.5, 1, 1e22, 1e-5, 0.0],
            "public": [True, False],
        }
        for field, values in samples.items():
            if op in ("$gt", "$gte", "$lt", "$lte") and field in ("lang", "public"):
                continue
            for v in values:
                value = [v, values[0]] if op in ("$in", "$nin") else v
                f = parse_filter({field: {op: value}}, full_schema())
                assert f is not None
                assert self.selected(table, compile_datafusion(f)) == self.expected(f), (field, op, value)

    @pytest.mark.parametrize("value", HOSTILE_STRINGS)
    def test_hostile_values_do_not_change_the_query(self, table: Any, value: str) -> None:
        for op in ("$eq", "$ne", "$in", "$nin"):
            v: Any = [value] if op in ("$in", "$nin") else value
            f = parse_filter({"lang": {op: v}}, full_schema())
            assert f is not None
            assert self.selected(table, compile_datafusion(f)) == self.expected(f)
        assert table.count_rows() == len(self.ROWS)  # nothing was dropped or deleted

    def test_random_filters_agree_with_the_reference_evaluator(self, table: Any) -> None:
        rng = random.Random(11)
        for _ in range(150):
            f = parse_filter(random_filter(rng), full_schema())
            assert f is not None
            assert self.selected(table, compile_datafusion(f)) == self.expected(f), f

    def test_missing_field_satisfies_only_ne_and_nin(self, table: Any) -> None:
        # Row 4 has NULL year/score/public; row 5 has NULL lang.
        assert 4 in self.selected(table, compile_datafusion(c("year", "$ne", 2020)))
        assert 4 in self.selected(table, compile_datafusion(c("year", "$nin", (2020,))))
        for op in ("$eq", "$gt", "$gte", "$lt", "$lte"):
            assert 4 not in self.selected(table, compile_datafusion(c("year", op, 2020)))
        assert 4 not in self.selected(table, compile_datafusion(c("year", "$in", (2020,))))

    def test_case_and_keyword_field_names_are_quoted(self, tmp_path: Path) -> None:
        import lancedb
        import pyarrow as pa

        t = lancedb.connect(str(tmp_path / "db")).create_table(
            "t", data=pa.table({"Year": [1, 2], "order": ["a", "b"]})
        )
        assert t.count_rows(compile_datafusion(c("Year", "$eq", 2))) == 1
        assert t.count_rows(compile_datafusion(c("order", "$eq", "b"))) == 1


# ---- configuration --------------------------------------------------------------------------


def test_config_defaults_and_validation() -> None:
    cfg = LanceDBConfig()
    assert cfg.uri is None and cfg.ann_index_min_rows == 50_000 and cfg.index_type == "IVF_FLAT" and cfg.prefix == ""
    assert LanceDBConfig(ann_index_min_rows=None).ann_index_min_rows is None
    for bad in (
        {"ann_index_min_rows": 10},
        {"ann_index_min_rows": 0},
        {"index_type": "HNSW"},
        {"nprobes": 0},
        {"prefix": "a b"},
        {"prefix": "a/b"},
        {"prefix": "a;b"},
        {"uri": "  "},
        {"surprise": 1},
    ):
        with pytest.raises(ValueError):
            LanceDBConfig.model_validate(bad)


def test_store_type_registration() -> None:
    assert STORE_TYPE.type == "lancedb"
    assert STORE_TYPE.secret_keys == ("api_key",)
    assert set(STORE_TYPE.immutable_fields) == {"ann_index_min_rows", "index_type"}
    assert set(STORE_TYPE.immutable_fields) <= set(LanceDBConfig.model_fields)
    assert STORE_TYPE.capabilities.metrics == frozenset({"cosine", "l2", "dot"})


def test_importing_the_adapter_needs_no_client() -> None:
    code = (
        "import sys, ragcentre.stores.lancedb\n"
        "assert 'lancedb' not in sys.modules and 'pyarrow' not in sys.modules, sorted(sys.modules)\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_missing_extra_is_a_clear_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "lancedb", None)  # makes `import lancedb` raise ImportError
    with pytest.raises(StoreUnavailable, match="'lancedb' extra"):
        LanceDBStore.connect(str(tmp_path / "x"))
    with pytest.raises(StoreUnavailable):
        STORE_TYPE.factory(LanceDBConfig(), {}, tmp_path)


@needs_lancedb
def test_factory_places_relative_uris_in_the_data_dir(tmp_path: Path) -> None:
    for config, expected in [({}, "lancedb"), ({"uri": "my/tables"}, "my/tables")]:
        s = STORE_TYPE.factory(LanceDBConfig.model_validate(config), {}, tmp_path)
        try:
            assert isinstance(s, VectorStore)
            s.healthcheck()
            s.ensure_collection(spec())
        finally:
            s.close()
        assert (tmp_path / "stores" / expected / f"{NAME}.lance").is_dir()


@needs_lancedb
def test_factory_passes_secret_and_region_to_the_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import lancedb

    seen: list[tuple[Any, dict[str, Any]]] = []

    class FakeDB:
        def close(self) -> None:
            pass

    def fake_connect(uri: Any, **kwargs: Any) -> FakeDB:
        seen.append((uri, kwargs))
        return FakeDB()

    monkeypatch.setattr(lancedb, "connect", fake_connect)
    s = STORE_TYPE.factory(LanceDBConfig(uri="db://mydb", region="eu-west-1"), {"api_key": "k-123"}, tmp_path)
    assert seen[-1] == ("db://mydb", {"api_key": "k-123", "region": "eu-west-1"})
    # The cloud service has not been run against: conservative guarantees.
    assert s.capabilities.filter_exact is False and s.capabilities.consistency == "eventual"
    s = STORE_TYPE.factory(LanceDBConfig(uri="s3://bucket/path", region="eu-west-1"), {}, tmp_path)
    assert seen[-1] == ("s3://bucket/path", {"storage_options": {"aws_region": "eu-west-1"}})
    assert s.capabilities.filter_exact is True and s.capabilities.consistency == "immediate"


@needs_lancedb
def test_local_capabilities(store: LanceDBStore) -> None:
    caps = store.capabilities
    assert caps.metrics == frozenset({"cosine", "l2", "dot"})
    assert caps.filter_exact and caps.consistency == "immediate" and caps.max_dim is None
    assert isinstance(store, VectorStore)


# ---- scores ---------------------------------------------------------------------------------


def test_score_transforms() -> None:
    assert score_from_distance("cosine", 0.25) == pytest.approx(0.75)
    assert score_from_distance("dot", -6.0) == pytest.approx(7.0)
    assert score_from_distance("l2", 25.0) == pytest.approx(-5.0)  # squared distance 25 is distance 5
    assert score_from_distance("l2", -1e-9) == 0.0  # float noise cannot make a square root fail


@needs_lancedb
def test_scores_per_metric_on_known_vectors(store: LanceDBStore) -> None:
    ids = [str(uuid.uuid4()) for _ in range(2)]
    vectors = [[3.0, 0.0], [3.0, 4.0]]
    for metric, query, expected in [
        ("cosine", [1.0, 0.0], [1.0, 0.6]),
        ("dot", [2.0, 0.0], [6.0, 6.0]),
        ("l2", [3.0, 0.0], [0.0, -4.0]),
    ]:
        name = f"rc_known_{metric}"
        store.ensure_collection(CollectionSpec(name, 2, metric))  # type: ignore[arg-type]
        store.upsert(name, [Record(i, v, {}) for i, v in zip(ids, vectors, strict=True)])
        got = {m.id: m.score for m in store.query(name, query, 5)}
        for rid, want in zip(ids, expected, strict=True):
            assert got[rid] == pytest.approx(want, abs=1e-5), (metric, rid)


@needs_lancedb
def test_l2_is_the_euclidean_distance_not_its_square(store: LanceDBStore) -> None:
    store.ensure_collection(CollectionSpec("rc_l2_0001", 2, "l2"))
    store.upsert("rc_l2_0001", [Record(str(uuid.uuid4()), [3.0, 4.0], {})])
    [m] = store.query("rc_l2_0001", [0.0, 0.0], 1)
    assert m.score == pytest.approx(-5.0)


# ---- catalog (schema metadata + columns) ----------------------------------------------------


@needs_lancedb
def test_catalog_round_trip_through_a_second_instance(tmp_path: Path) -> None:
    uri = str(tmp_path / "lance")
    a = LanceDBStore.connect(uri, index_type="IVF_SQ", ann_index_min_rows=2000)
    a.ensure_collection(spec(metric="l2", fields={"lang": "str", "year": "int"}))
    a.close()
    b = LanceDBStore.connect(uri, index_type="IVF_SQ", ann_index_min_rows=2000)
    try:
        info = b.collection_info(NAME)
        assert info is not None and (info.dim, info.metric, info.count) == (DIM, "l2", 0)
        table, entry = b._require(NAME)
        assert entry.fields == {"document_id": "str", "lang": "str", "year": "int"}
        assert entry.options == {"index_type": "IVF_SQ", "ann_index_min_rows": 2000}
        assert META_KEY in table.schema.metadata
        # Same spec again is idempotent; a different metric or dimension is a mismatch.
        b.ensure_collection(spec(metric="l2", fields={"lang": "str", "year": "int"}))
        with pytest.raises(CollectionMismatch):
            b.ensure_collection(spec(metric="cosine", fields={"lang": "str"}))
        with pytest.raises(CollectionMismatch):
            b.ensure_collection(spec(dim=DIM + 1, metric="l2", fields={"lang": "str"}))
    finally:
        b.close()


@needs_lancedb
def test_other_index_options_are_a_mismatch(tmp_path: Path) -> None:
    uri = str(tmp_path / "lance")
    a = LanceDBStore.connect(uri)
    a.ensure_collection(spec())
    a.close()
    for options in ({"index_type": "IVF_PQ"}, {"ann_index_min_rows": 1000}, {"ann_index_min_rows": None}):
        b = LanceDBStore.connect(uri, **options)
        try:
            with pytest.raises(CollectionMismatch, match="options"):
                b.ensure_collection(spec())
        finally:
            b.close()


@needs_lancedb
def test_adding_fields_keeps_schema_metadata_and_data(store: LanceDBStore) -> None:
    store.ensure_collection(spec(fields={"lang": "str"}))
    recs = records(20)
    store.upsert(NAME, recs)
    table, before = store._require(NAME)
    meta_before = dict(table.schema.metadata)
    store.ensure_collection(spec(fields={"lang": "str", "year": "int", "score": "float", "public": "bool"}))
    table, after = store._require(NAME)
    assert after.fields == {"document_id": "str", "lang": "str", "year": "int", "score": "float", "public": "bool"}
    assert dict(table.schema.metadata) == meta_before
    assert (after.dim, after.metric, after.options) == (before.dim, before.metric, before.options)
    # Old rows have NULL for the new fields: only $ne/$nin match them, as the reference evaluator says.
    q = [0.1] * DIM
    schema = full_schema()
    assert store.query(NAME, q, 50, parse_filter({"year": {"$gte": 0}}, schema)) == []
    assert len(store.query(NAME, q, 50, parse_filter({"year": {"$ne": 2020}}, schema))) == 20
    assert len(store.query(NAME, q, 50, parse_filter({"year": {"$nin": [1]}}, schema))) == 20
    # Rewriting the records (what the core does after a schema change) fills them in.
    store.upsert(NAME, recs)
    assert len(store.query(NAME, q, 50, parse_filter({"year": {"$gte": 2020}}, schema))) == 20
    assert store.collection_info(NAME).count == 20  # type: ignore[union-attr]


@needs_lancedb
def test_field_type_change_is_a_mismatch(store: LanceDBStore) -> None:
    store.ensure_collection(spec(fields={"year": "int"}))
    with pytest.raises(CollectionMismatch, match="year"):
        store.ensure_collection(spec(fields={"year": "float"}))


@needs_lancedb
def test_reserved_column_names_are_rejected(store: LanceDBStore) -> None:
    with pytest.raises(StoreError, match="reserved"):
        store.ensure_collection(spec(fields={"_rc_id": "str"}))
    assert store.collection_info(NAME) is None
    # Fields named like LanceDB's own conventions are fine: the store's columns are prefixed.
    store.ensure_collection(spec(fields={"id": "str", "vector": "int", "Year": "int"}))
    store.upsert(NAME, [Record(str(uuid.uuid4()), [1.0] * DIM, {"id": "x", "vector": 3, "Year": 5})])
    f = parse_filter({"Year": 5, "id": "x"}, {"Year": "int", "id": "str", "document_id": "str", "vector": "int"})
    assert len(store.query(NAME, [1.0] * DIM, 3, f)) == 1


@needs_lancedb
def test_a_table_not_made_by_ragcentre_is_refused(tmp_path: Path) -> None:
    import lancedb
    import pyarrow as pa

    uri = str(tmp_path / "lance")
    lancedb.connect(uri).create_table(NAME, data=pa.table({"x": [1]}))
    s = LanceDBStore.connect(uri)
    try:
        with pytest.raises(StoreError, match="not created by RAGCentre"):
            s.ensure_collection(spec())
        with pytest.raises(StoreError, match="not created by RAGCentre"):
            s.collection_info(NAME)
    finally:
        s.close()


@needs_lancedb
def test_concurrent_ensure_creates_one_collection(tmp_path: Path) -> None:
    import threading

    uri = str(tmp_path / "lance")
    stores = [LanceDBStore.connect(uri) for _ in range(4)]  # separate handles, as separate workers would have
    errors: list[BaseException] = []

    def work(s: LanceDBStore) -> None:
        try:
            s.ensure_collection(spec(fields={"lang": "str", "year": "int"}))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    try:
        assert not errors, errors
        info = stores[0].collection_info(NAME)
        assert info is not None and info.dim == DIM
    finally:
        for s in stores:
            s.close()


# ---- behaviour at the edges -----------------------------------------------------------------


@needs_lancedb
def test_missing_collection(store: LanceDBStore) -> None:
    with pytest.raises(CollectionNotFound):
        store.query("rc_missing_0001", [1.0] * DIM, 3)
    with pytest.raises(CollectionNotFound):
        store.upsert("rc_missing_0001", records(1))
    store.delete("rc_missing_0001", [str(uuid.uuid4())])
    store.drop_collection("rc_missing_0001")
    assert store.collection_info("rc_missing_0001") is None


@needs_lancedb
def test_upsert_validation(store: LanceDBStore) -> None:
    store.ensure_collection(spec())
    with pytest.raises(StoreError, match="dimension"):
        store.upsert(NAME, records(1, dim=DIM + 1))
    with pytest.raises(StoreError, match="non-finite"):
        store.upsert(NAME, [Record(str(uuid.uuid4()), [math.nan] * DIM, {})])
    with pytest.raises(StoreError, match="max_batch"):
        store.upsert(NAME, records(store.capabilities.max_batch + 1))
    with pytest.raises(StoreError, match="does not fit"):
        store.upsert(NAME, [Record(str(uuid.uuid4()), [1.0] * DIM, {"year": "not a number"})])
    store.upsert(NAME, [])
    assert store.collection_info(NAME).count == 0  # type: ignore[union-attr]
    with pytest.raises(StoreError, match="dimension"):
        store.query(NAME, [1.0], 3)
    assert store.query(NAME, [1.0] * DIM, 0) == []


@needs_lancedb
def test_duplicate_ids_in_a_batch_keep_the_last(store: LanceDBStore) -> None:
    store.ensure_collection(spec())
    rid = str(uuid.uuid4())
    store.upsert(NAME, [Record(rid, [1.0] * DIM, {"lang": "en"}), Record(rid, [-1.0] * DIM, {"lang": "fr"})])
    assert store.collection_info(NAME).count == 1  # type: ignore[union-attr]
    got = store.query(NAME, [-1.0] * DIM, 1, parse_filter({"lang": "fr"}, full_schema()))
    assert [m.id for m in got] == [rid]


@needs_lancedb
def test_metadata_keys_outside_the_schema_are_dropped(store: LanceDBStore) -> None:
    store.ensure_collection(spec(fields={"lang": "str"}))
    store.upsert(NAME, [Record(str(uuid.uuid4()), [1.0] * DIM, {"lang": "en", "stray": 1})])
    assert "stray" not in store._require(NAME)[1].fields


@needs_lancedb
def test_hostile_ids_cannot_escape_the_delete_predicate(store: LanceDBStore) -> None:
    store.ensure_collection(spec())
    store.upsert(NAME, records(5))
    store.delete(NAME, ["x' OR 1=1 --", "'); DELETE FROM t; --", "\\"])
    assert store.collection_info(NAME).count == 5  # type: ignore[union-attr]


@needs_lancedb
def test_zero_vectors_do_not_break_cosine_search(store: LanceDBStore) -> None:
    # LanceDB leaves a zero vector out of cosine results (it has no cosine distance); document that
    # it is at least not an error and never a wrong score.
    store.ensure_collection(spec())
    rid, zid = str(uuid.uuid4()), str(uuid.uuid4())
    store.upsert(NAME, [Record(rid, [1.0] * DIM, {}), Record(zid, [0.0] * DIM, {})])
    got = {m.id: m.score for m in store.query(NAME, [1.0] * DIM, 5)}
    assert got[rid] == pytest.approx(1.0, abs=1e-5)
    assert got.get(zid, 0.0) == pytest.approx(0.0, abs=1e-5)


@needs_lancedb
def test_prefix_maps_to_the_physical_table_name(tmp_path: Path) -> None:
    uri = str(tmp_path / "lance")
    a, b = LanceDBStore.connect(uri, prefix="tenant_a_"), LanceDBStore.connect(uri, prefix="tenant_b_")
    try:
        a.ensure_collection(spec())
        assert b.collection_info(NAME) is None  # the other prefix does not see it
        b.ensure_collection(spec(dim=3))
        assert a.collection_info(NAME).dim == DIM  # type: ignore[union-attr]
        assert b.collection_info(NAME).dim == 3  # type: ignore[union-attr]
        assert sorted(p.name for p in (tmp_path / "lance").iterdir()) == [
            f"tenant_a_{NAME}.lance",
            f"tenant_b_{NAME}.lance",
        ]
        a.drop_collection(NAME)
        assert b.collection_info(NAME) is not None
    finally:
        a.close()
        b.close()


@needs_lancedb
def test_a_prefix_that_makes_the_name_invalid_fails_clearly(tmp_path: Path) -> None:
    s = LanceDBStore.connect(str(tmp_path / "lance"), prefix="a/")
    try:
        with pytest.raises(StoreError, match="valid LanceDB table name"):
            s.ensure_collection(spec())
    finally:
        s.close()


@needs_lancedb
def test_healthcheck_reports_an_unusable_database(tmp_path: Path) -> None:
    s = LanceDBStore.connect(str(tmp_path / "lance"))
    try:
        s.healthcheck()
        (tmp_path / "lance").rmdir()
        (tmp_path / "lance").write_text("a file where the database directory should be")
        with pytest.raises(StoreError, match="not reachable"):
            s.healthcheck()
    finally:
        s.close()


# ---- ANN index threshold --------------------------------------------------------------------


def brute_top(recs: list[Record], q: list[float], k: int) -> list[str]:
    def cos(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True)) / (math.hypot(*a) * math.hypot(*b))

    return [r.id for r in sorted(recs, key=lambda r: -cos(r.vector, q))[:k]]


@needs_lancedb
@pytest.mark.parametrize("threshold", [None, 5000])
def test_below_the_threshold_no_index_is_built_and_search_is_exact(tmp_path: Path, threshold: int | None) -> None:
    s = LanceDBStore.connect(str(tmp_path / "lance"), ann_index_min_rows=threshold)
    try:
        s.ensure_collection(spec())
        recs = records(600)
        s.upsert(NAME, recs[:300])
        s.upsert(NAME, recs[300:])
        assert not has_vector_index(s)
        rng = random.Random(3)
        for _ in range(5):
            q = [rng.uniform(-1, 1) for _ in range(DIM)]
            assert [m.id for m in s.query(NAME, q, 10)] == brute_top(recs, q, 10)
    finally:
        s.close()


@needs_lancedb
@pytest.mark.parametrize("index_type", ["IVF_FLAT", "IVF_SQ", "IVF_PQ", "IVF_HNSW_SQ"])
def test_reaching_the_threshold_builds_an_index_once(tmp_path: Path, index_type: str) -> None:
    s = LanceDBStore.connect(str(tmp_path / "lance"), ann_index_min_rows=300, index_type=index_type)  # type: ignore[arg-type]
    try:
        s.ensure_collection(spec())
        recs = records(500)
        s.upsert(NAME, recs[:250])
        assert not has_vector_index(s)  # still below the threshold
        s.upsert(NAME, recs[250:350])
        assert has_vector_index(s)  # the upsert that crossed it built the index
        table, _ = s._require(NAME)
        [ix] = [i for i in table.list_indices() if i.name == INDEX_NAME]
        assert index_type.replace("_", "").lower() in ix.index_type.lower().replace("_", "")
        assert table.index_stats(INDEX_NAME).distance_type == "cosine"
        s.upsert(NAME, recs[350:])  # later rows are searched even before the index is refreshed
        assert s.collection_info(NAME).count == 500  # type: ignore[union-attr]
        newest = recs[-1]
        assert s.query(NAME, newest.vector, 1)[0].id == newest.id
    finally:
        s.close()


@needs_lancedb
def test_with_an_index_results_are_filtered_before_ranking_and_never_under_filled(tmp_path: Path) -> None:
    s = LanceDBStore.connect(str(tmp_path / "lance"), ann_index_min_rows=300, nprobes=1)
    try:
        s.ensure_collection(spec())
        recs = [
            Record(r.id, r.vector, {**r.metadata, "lang": "rare" if i % 50 == 0 else "en"})
            for i, r in enumerate(records(1000))
        ]
        s.upsert(NAME, recs)
        assert has_vector_index(s)
        rng = random.Random(5)
        rare = parse_filter({"lang": "rare"}, full_schema())
        for k in (2, 10, 40):
            q = [rng.uniform(-1, 1) for _ in range(DIM)]
            got = s.query(NAME, q, k, rare)
            assert len(got) == min(k, 20)  # k matches come back whenever k exist
            by_id = {r.id: r for r in recs}
            assert all(by_id[m.id].metadata["lang"] == "rare" for m in got)
        # Unfiltered ANN search is approximate, but close: recall against brute force.
        hits = 0
        for _ in range(10):
            q = [rng.uniform(-1, 1) for _ in range(DIM)]
            hits += len(set(m.id for m in s.query(NAME, q, 10)) & set(brute_top(recs, q, 10)))
        assert hits >= 60  # at least 60% recall with a single probe
    finally:
        s.close()


@needs_lancedb
def test_index_maintenance_failure_does_not_fail_the_upsert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    s = LanceDBStore.connect(str(tmp_path / "lance"), ann_index_min_rows=300)
    try:
        s.ensure_collection(spec())

        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("index build exploded")

        monkeypatch.setattr(s, "_build_index", boom)
        s.upsert(NAME, records(400))
        assert s.collection_info(NAME).count == 400  # type: ignore[union-attr]
        assert "index build exploded" in caplog.text
        assert not has_vector_index(s)
    finally:
        s.close()
