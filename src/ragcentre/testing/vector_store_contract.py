"""Reusable contract suite for vector store adapters (section 10.4).

Subclass ``VectorStoreContract`` in a test module and provide a ``store`` fixture that returns
a fresh, empty store::

    class TestMyStore(VectorStoreContract):
        @pytest.fixture
        def store(self, tmp_path):
            s = MyStore(...)
            yield s
            s.close()

The suite checks results against a brute-force computation in pure Python, with filters
evaluated by ``ragcentre.models.filters.matches``.
"""

from __future__ import annotations

import itertools
import random
import threading
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import Filter, matches, parse_filter
from ragcentre.stores.base import (
    CollectionSpec,
    Match,
    Metric,
    Record,
    VectorStore,
    upsert_batched,
)
from ragcentre.testing.random_data import (
    SCHEMA_FIELDS,
    full_schema,
    random_condition,
    random_filter,
    random_metadata,
    random_vector,
)
from ragcentre.vectors import cosine_similarity, dot, float32, l2_distance

DIM = 8
N_RECORDS = 200
SEED = 20240611
TOL = 1e-4


def brute_score(metric: Metric, a: Sequence[float], b: Sequence[float]) -> float:
    if metric == "cosine":
        return cosine_similarity(a, b)
    if metric == "l2":
        return -l2_distance(a, b)
    return dot(a, b)


def make_records(rng: random.Random, n: int = N_RECORDS, dim: int = DIM) -> list[Record]:
    return [
        Record(id=str(uuid.UUID(int=rng.getrandbits(128), version=4)), vector=float32(random_vector(rng, dim)),
               metadata=random_metadata(rng))
        for _ in range(n)
    ]  # fmt: skip


def expected_top_k(
    records: Sequence[Record], query: Sequence[float], k: int, metric: Metric, f: Filter | None
) -> list[tuple[str, float]]:
    scored = [(r.id, brute_score(metric, r.vector, query)) for r in records if matches(f, r.metadata)]
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:k]


def assert_same_results(
    got: list[Match], records: Sequence[Record], query: Sequence[float], k: int, metric: Metric, f: Filter | None
) -> None:
    """Exact equality is too strict across float32/float64, so compare robustly:
    the count is right, every id passes the filter, scores are sorted, each score equals its
    brute-force score, and the score sequence equals the brute-force top-k sequence."""
    want = expected_top_k(records, query, k, metric, f)
    by_id = {r.id: r for r in records}
    assert len(got) == len(want), f"expected {len(want)} matches, got {len(got)}"
    assert len({m.id for m in got}) == len(got), "duplicate ids returned"
    for m in got:
        assert m.id in by_id, f"unknown id {m.id}"
        assert matches(f, by_id[m.id].metadata), f"id {m.id} does not satisfy the filter"
        assert m.score == pytest.approx(brute_score(metric, by_id[m.id].vector, query), abs=TOL)
    scores = [m.score for m in got]
    assert all(a >= b - TOL for a, b in itertools.pairwise(scores)), "scores not sorted descending"
    assert scores == pytest.approx([s for _, s in want], abs=TOL)


class VectorStoreContract:
    """Base class; subclasses provide the ``store`` fixture."""

    @pytest.fixture
    def store(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError("provide a `store` fixture returning a fresh VectorStore")

    # ---- helpers ------------------------------------------------------------------------------

    @staticmethod
    def metrics(store: VectorStore) -> list[Metric]:
        return sorted(store.capabilities.metrics)

    @staticmethod
    def spec(name: str = "rc_contract_0001", dim: int = DIM, metric: Metric = "cosine", **kw: Any) -> CollectionSpec:
        return CollectionSpec(name=name, dim=dim, metric=metric, fields=kw.get("fields", dict(SCHEMA_FIELDS)))

    def _loaded(self, store: VectorStore, metric: Metric, name: str = "rc_contract_0001") -> list[Record]:
        rng = random.Random(SEED)
        records = make_records(rng)
        store.ensure_collection(self.spec(name=name, metric=metric))
        upsert_batched(store, name, records)
        return records

    # ---- collections --------------------------------------------------------------------------

    def test_healthcheck(self, store: VectorStore) -> None:
        store.healthcheck()

    def test_ensure_collection_is_idempotent(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        store.ensure_collection(self.spec(metric=metric))
        store.ensure_collection(self.spec(metric=metric))
        info = store.collection_info("rc_contract_0001")
        assert info is not None
        assert (info.dim, info.metric, info.count) == (DIM, metric, 0)

    def test_dimension_mismatch(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        store.ensure_collection(self.spec(metric=metric))
        with pytest.raises(CollectionMismatch):
            store.ensure_collection(self.spec(dim=DIM + 1, metric=metric))

    def test_metric_mismatch(self, store: VectorStore) -> None:
        metrics = self.metrics(store)
        if len(metrics) < 2:
            pytest.skip("store supports a single metric")
        store.ensure_collection(self.spec(metric=metrics[0]))
        with pytest.raises(CollectionMismatch):
            store.ensure_collection(self.spec(metric=metrics[1]))

    def test_adding_a_field(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        store.ensure_collection(self.spec(metric=metric, fields={"lang": "str"}))
        store.ensure_collection(self.spec(metric=metric, fields={"lang": "str", "year": "int"}))
        rng = random.Random(1)
        records = make_records(rng, 20)
        upsert_batched(store, "rc_contract_0001", records)
        f = parse_filter({"year": {"$gte": 2020}}, full_schema())
        q = random_vector(rng, DIM)
        got = store.query("rc_contract_0001", q, 5, f)
        assert_same_results(got, records, q, 5, metric, f)

    def test_collection_info_missing(self, store: VectorStore) -> None:
        assert store.collection_info("rc_missing_0001") is None

    # ---- writes -------------------------------------------------------------------------------

    def test_upsert_overwrite_and_count(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        store.ensure_collection(self.spec(metric=metric))
        rng = random.Random(2)
        records = make_records(rng, 10)
        store.upsert("rc_contract_0001", records)
        info = store.collection_info("rc_contract_0001")
        assert info is not None and info.count == 10
        target = records[0]
        replacement = Record(id=target.id, vector=float32(random_vector(rng, DIM)),
                             metadata={**target.metadata, "lang": "zz"})  # fmt: skip
        store.upsert("rc_contract_0001", [replacement])
        info = store.collection_info("rc_contract_0001")
        assert info is not None and info.count == 10
        got = store.query("rc_contract_0001", replacement.vector, 1, parse_filter({"lang": "zz"}, full_schema()))
        assert [m.id for m in got] == [target.id]
        assert got[0].score == pytest.approx(brute_score(metric, replacement.vector, replacement.vector), abs=TOL)

    def test_upsert_more_than_max_batch(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        store.ensure_collection(self.spec(metric=metric))
        n = store.capabilities.max_batch * 2 + 7
        rng = random.Random(3)
        records = make_records(rng, n)
        calls: list[int] = []

        class Counting:
            capabilities = store.capabilities

            def upsert(self, name: str, recs: Sequence[Record]) -> None:
                calls.append(len(recs))
                store.upsert(name, recs)

        upsert_batched(Counting(), "rc_contract_0001", records)  # type: ignore[arg-type]
        assert max(calls) <= store.capabilities.max_batch
        info = store.collection_info("rc_contract_0001")
        assert info is not None and info.count == n

    def test_delete_existing_and_missing(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        records = self._loaded(store, metric)
        store.delete("rc_contract_0001", [records[0].id, records[1].id, str(uuid.uuid4())])
        store.delete("rc_contract_0001", [])
        info = store.collection_info("rc_contract_0001")
        assert info is not None and info.count == N_RECORDS - 2
        got = store.query("rc_contract_0001", records[0].vector, N_RECORDS)
        assert records[0].id not in {m.id for m in got}

    def test_drop_existing_and_missing(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        self._loaded(store, metric)
        store.drop_collection("rc_contract_0001")
        assert store.collection_info("rc_contract_0001") is None
        store.drop_collection("rc_contract_0001")
        store.drop_collection("rc_never_existed")
        # Re-creating after a drop gives an empty collection, even with another dimension.
        store.ensure_collection(self.spec(dim=DIM + 2, metric=metric))
        info = store.collection_info("rc_contract_0001")
        assert info is not None and (info.dim, info.count) == (DIM + 2, 0)

    # ---- queries ------------------------------------------------------------------------------

    def test_query_ordering_per_metric(self, store: VectorStore) -> None:
        for i, metric in enumerate(self.metrics(store)):
            name = f"rc_metric_{i:04d}"
            records = self._loaded(store, metric, name=name)
            rng = random.Random(SEED + i)
            for _ in range(10):
                q = random_vector(rng, DIM)
                got = store.query(name, q, 10)
                assert_same_results(got, records, q, 10, metric, None)

    def test_k_larger_than_collection(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        records = self._loaded(store, metric)
        q = random_vector(random.Random(5), DIM)
        got = store.query("rc_contract_0001", q, N_RECORDS + 50)
        assert len(got) == N_RECORDS
        assert_same_results(got, records, q, N_RECORDS + 50, metric, None)

    @pytest.mark.parametrize("op", ["$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin"])
    def test_every_filter_operator(self, store: VectorStore, op: str) -> None:
        metric = self.metrics(store)[0]
        records = self._loaded(store, metric)
        rng = random.Random(f"{SEED}{op}")
        schema = full_schema()
        fields = [f for f, t in schema.items() if op not in ("$gt", "$gte", "$lt", "$lte") or t in ("int", "float")]
        for field in fields:
            for _ in range(3):
                raw = random_condition(rng, op=op, field=field)
                f = parse_filter(raw, schema)
                q = random_vector(rng, DIM)
                got = store.query("rc_contract_0001", q, 10, f)
                assert_same_results(got, records, q, 10, metric, f)

    @pytest.mark.parametrize("combinator", ["$and", "$or", "random"])
    def test_combinators(self, store: VectorStore, combinator: str) -> None:
        metric = self.metrics(store)[0]
        records = self._loaded(store, metric)
        rng = random.Random(f"{SEED}{combinator}")
        schema = full_schema()
        for _ in range(25):
            if combinator == "random":
                raw = random_filter(rng)
            else:
                raw = {combinator: [random_condition(rng) for _ in range(rng.randint(2, 3))]}
            f = parse_filter(raw, schema)
            q = random_vector(rng, DIM)
            got = store.query("rc_contract_0001", q, 10, f)
            assert_same_results(got, records, q, 10, metric, f)

    def test_selective_filter_returns_min_k_matching(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        rng = random.Random(SEED)
        records = make_records(rng)
        # About 2% of records carry a rare value.
        rare = [i for i in range(0, N_RECORDS, 50)]
        records = [
            Record(r.id, r.vector, {**r.metadata, "lang": "rare"}) if i in rare else r for i, r in enumerate(records)
        ]
        store.ensure_collection(self.spec(metric=metric))
        upsert_batched(store, "rc_contract_0001", records)
        f = parse_filter({"lang": "rare"}, full_schema())
        for k in (2, 10):
            q = random_vector(rng, DIM)
            got = store.query("rc_contract_0001", q, k, f)
            assert len(got) == min(k, len(rare))
            assert_same_results(got, records, q, k, metric, f)

    def test_concurrent_upserts_and_queries(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        store.ensure_collection(self.spec(metric=metric))
        errors: list[BaseException] = []

        def work(seed: int) -> None:
            try:
                rng = random.Random(seed)
                for _ in range(10):
                    store.upsert("rc_contract_0001", make_records(rng, 10))
                    store.query("rc_contract_0001", random_vector(rng, DIM), 5,
                                parse_filter({"public": True}, full_schema()))  # fmt: skip
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not errors, errors
        info = store.collection_info("rc_contract_0001")
        assert info is not None and info.count == 400
