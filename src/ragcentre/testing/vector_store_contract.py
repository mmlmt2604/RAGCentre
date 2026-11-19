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
evaluated by ``ragcentre.models.filters.matches``. By default every query must return the exact
top-k.

Hooks an adapter's test class may override:

* ``settle(store, name)``: called after every write (ensure, upsert, delete, drop). Default no-op.
  Stores with ``consistency="eventual"`` wait here until the writes are visible, for example by
  polling ``collection_info(name).count`` (see ``wait_until``) or refreshing an index. It must
  return at once when there is nothing to wait for, and tolerate a collection that does not exist.
* ``score_tolerance``: the absolute score error allowed against brute force (default ``TOL``).
  Raise it for stores whose score transform loses float precision, and say why in a comment.
* ``filter_min_recall``: with ``filter_exact=False`` the filtered-query tests check recall against
  brute force (at least 0.9) instead of the exact top-k. Every returned id must still satisfy the
  filter, and scores and ordering are still checked. The test report records the mode.

Adapters with immutable index options also mix in ``CollectionOptionsContract``.
"""

from __future__ import annotations

import itertools
import random
import threading
import time
import uuid
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import Filter, matches, parse_filter
from ragcentre.stores.base import (
    METRICS,
    Capabilities,
    CollectionSpec,
    Match,
    Metric,
    Record,
    StoreError,
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
MIN_RECALL = 0.9  # lowest recall a filter_exact=False adapter may be held to


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


def wait_until(
    predicate: Callable[[], bool], *, timeout: float = 30.0, interval: float = 0.05, what: str = "condition"
) -> None:
    """Poll ``predicate`` until it is true; for ``settle`` implementations of eventual stores."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(interval)


def assert_same_results(
    got: list[Match],
    records: Sequence[Record],
    query: Sequence[float],
    k: int,
    metric: Metric,
    f: Filter | None,
    tol: float = TOL,
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
        assert m.score == pytest.approx(brute_score(metric, by_id[m.id].vector, query), abs=tol)
    scores = [m.score for m in got]
    assert all(a >= b - tol for a, b in itertools.pairwise(scores)), "scores not sorted descending"
    assert scores == pytest.approx([s for _, s in want], abs=tol)


def assert_recall(
    got: list[Match],
    records: Sequence[Record],
    query: Sequence[float],
    k: int,
    metric: Metric,
    f: Filter | None,
    tol: float = TOL,
    min_recall: float = MIN_RECALL,
) -> None:
    """The check for filtered queries on a store with ``filter_exact=False``.

    Looser than ``assert_same_results`` in one way only: the returned ids need not be exactly the
    brute-force top-k, but at least ``min_recall`` of them must be. Everything else holds: no
    duplicates, at most ``k`` results, every id satisfies the filter, every score equals its
    brute-force score, and scores are sorted. (Filtering is never done after the search, so an
    id that fails the filter is always a bug, exact store or not.)
    """
    assert min_recall >= MIN_RECALL, f"a recall threshold below {MIN_RECALL} is not allowed"
    want = expected_top_k(records, query, k, metric, f)
    by_id = {r.id: r for r in records}
    assert len(got) <= k, f"expected at most {k} matches, got {len(got)}"
    assert len({m.id for m in got}) == len(got), "duplicate ids returned"
    for m in got:
        assert m.id in by_id, f"unknown id {m.id}"
        assert matches(f, by_id[m.id].metadata), f"id {m.id} does not satisfy the filter"
        assert m.score == pytest.approx(brute_score(metric, by_id[m.id].vector, query), abs=tol)
    scores = [m.score for m in got]
    assert all(a >= b - tol for a, b in itertools.pairwise(scores)), "scores not sorted descending"
    if want:
        recall = len({m.id for m in got} & {i for i, _ in want}) / len(want)
        assert recall >= min_recall, f"recall {recall:.2f} below {min_recall} ({len(got)} of {len(want)} wanted)"


class VectorStoreContract:
    """Base class; subclasses provide the ``store`` fixture and may override the hooks below."""

    score_tolerance: ClassVar[float] = TOL
    filter_min_recall: ClassVar[float] = MIN_RECALL

    @pytest.fixture
    def store(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError("provide a `store` fixture returning a fresh VectorStore")

    def settle(self, store: VectorStore, name: str) -> None:
        """Wait until the writes made so far on ``name`` are visible to queries. Default: no-op."""

    @pytest.fixture(autouse=True)
    def _report_filter_mode(self, request: pytest.FixtureRequest, store: VectorStore) -> None:
        if not store.capabilities.filter_exact:
            mode = f"recall>={self.filter_min_recall}"
            request.node.user_properties.append(("filter_mode", mode))
            warnings.warn(
                f"filter_exact=False: filtered queries are checked for {mode} against brute force, "
                "not for the exact top-k",
                stacklevel=1,
            )

    # ---- helpers ------------------------------------------------------------------------------

    @staticmethod
    def metrics(store: VectorStore) -> list[Metric]:
        return sorted(store.capabilities.metrics)

    @staticmethod
    def spec(name: str = "rc_contract_0001", dim: int = DIM, metric: Metric = "cosine", **kw: Any) -> CollectionSpec:
        return CollectionSpec(name=name, dim=dim, metric=metric, fields=kw.get("fields", dict(SCHEMA_FIELDS)))

    def _ensure(self, store: VectorStore, spec: CollectionSpec) -> None:
        store.ensure_collection(spec)
        self.settle(store, spec.name)

    def _put(self, store: VectorStore, name: str, records: Sequence[Record]) -> None:
        upsert_batched(store, name, records)
        self.settle(store, name)

    def _check(
        self, store: VectorStore, got: list[Match], records: Sequence[Record], q: Sequence[float], k: int,
        metric: Metric, f: Filter | None,
    ) -> None:  # fmt: skip
        """Exact top-k, except filtered queries on a store that declares ``filter_exact=False``."""
        if f is not None and not store.capabilities.filter_exact:
            assert_recall(got, records, q, k, metric, f, self.score_tolerance, self.filter_min_recall)
        else:
            assert_same_results(got, records, q, k, metric, f, self.score_tolerance)

    def _loaded(self, store: VectorStore, metric: Metric, name: str = "rc_contract_0001") -> list[Record]:
        rng = random.Random(SEED)
        records = make_records(rng)
        self._ensure(store, self.spec(name=name, metric=metric))
        self._put(store, name, records)
        return records

    # ---- collections --------------------------------------------------------------------------

    def test_healthcheck(self, store: VectorStore) -> None:
        store.healthcheck()

    def test_ensure_collection_is_idempotent(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        self._ensure(store, self.spec(metric=metric))
        self._ensure(store, self.spec(metric=metric))
        info = store.collection_info("rc_contract_0001")
        assert info is not None
        assert (info.dim, info.metric, info.count) == (DIM, metric, 0)

    def test_dimension_mismatch(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        self._ensure(store, self.spec(metric=metric))
        other_dim = DIM - 1 if store.capabilities.max_dim == DIM else DIM + 1
        with pytest.raises(CollectionMismatch):
            store.ensure_collection(self.spec(dim=other_dim, metric=metric))

    def test_metric_mismatch(self, store: VectorStore) -> None:
        metrics = self.metrics(store)
        if len(metrics) < 2:
            pytest.skip("store supports a single metric")
        self._ensure(store, self.spec(metric=metrics[0]))
        with pytest.raises(CollectionMismatch):
            store.ensure_collection(self.spec(metric=metrics[1]))

    def test_adding_a_field(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        self._ensure(store, self.spec(metric=metric, fields={"lang": "str"}))
        self._ensure(store, self.spec(metric=metric, fields={"lang": "str", "year": "int"}))
        rng = random.Random(1)
        records = make_records(rng, 20)
        self._put(store, "rc_contract_0001", records)
        f = parse_filter({"year": {"$gte": 2020}}, full_schema())
        q = random_vector(rng, DIM)
        got = store.query("rc_contract_0001", q, 5, f)
        self._check(store, got, records, q, 5, metric, f)

    def test_collection_info_missing(self, store: VectorStore) -> None:
        assert store.collection_info("rc_missing_0001") is None

    def test_capabilities_are_well_formed(self, store: VectorStore) -> None:
        caps = store.capabilities
        assert isinstance(caps, Capabilities)
        assert caps.metrics and caps.metrics <= set(METRICS)
        assert caps.max_batch >= 1
        assert caps.consistency in ("immediate", "eventual")
        assert caps.max_dim is None or caps.max_dim >= 1
        assert caps.max_collections is None or caps.max_collections >= 1

    def test_max_dim_is_enforced(self, store: VectorStore) -> None:
        max_dim = store.capabilities.max_dim
        if max_dim is None:
            pytest.skip("store declares no max_dim")
        metric = self.metrics(store)[0]
        with pytest.raises(StoreError):
            store.ensure_collection(self.spec(dim=max_dim + 1, metric=metric))
        assert store.collection_info("rc_contract_0001") is None

    # ---- writes -------------------------------------------------------------------------------

    def test_upsert_overwrite_and_count(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        self._ensure(store, self.spec(metric=metric))
        rng = random.Random(2)
        records = make_records(rng, 10)
        store.upsert("rc_contract_0001", records)
        self.settle(store, "rc_contract_0001")
        info = store.collection_info("rc_contract_0001")
        assert info is not None and info.count == 10
        target = records[0]
        replacement = Record(id=target.id, vector=float32(random_vector(rng, DIM)),
                             metadata={**target.metadata, "lang": "zz"})  # fmt: skip
        store.upsert("rc_contract_0001", [replacement])
        self.settle(store, "rc_contract_0001")
        info = store.collection_info("rc_contract_0001")
        assert info is not None and info.count == 10
        got = store.query("rc_contract_0001", replacement.vector, 1, parse_filter({"lang": "zz"}, full_schema()))
        assert [m.id for m in got] == [target.id]
        assert got[0].score == pytest.approx(
            brute_score(metric, replacement.vector, replacement.vector), abs=self.score_tolerance
        )

    def test_upsert_more_than_max_batch(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        self._ensure(store, self.spec(metric=metric))
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
        self.settle(store, "rc_contract_0001")
        assert max(calls) <= store.capabilities.max_batch
        info = store.collection_info("rc_contract_0001")
        assert info is not None and info.count == n

    def test_delete_existing_and_missing(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        records = self._loaded(store, metric)
        store.delete("rc_contract_0001", [records[0].id, records[1].id, str(uuid.uuid4())])
        store.delete("rc_contract_0001", [])
        self.settle(store, "rc_contract_0001")
        info = store.collection_info("rc_contract_0001")
        assert info is not None and info.count == N_RECORDS - 2
        got = store.query("rc_contract_0001", records[0].vector, N_RECORDS)
        assert records[0].id not in {m.id for m in got}

    def test_drop_existing_and_missing(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        self._loaded(store, metric)
        store.drop_collection("rc_contract_0001")
        self.settle(store, "rc_contract_0001")
        assert store.collection_info("rc_contract_0001") is None
        store.drop_collection("rc_contract_0001")
        store.drop_collection("rc_never_existed")
        self.settle(store, "rc_never_existed")
        # Re-creating after a drop gives an empty collection, even with another dimension.
        limit = store.capabilities.max_dim
        other_dim = DIM - 2 if limit is not None and limit < DIM + 2 else DIM + 2
        self._ensure(store, self.spec(dim=other_dim, metric=metric))
        info = store.collection_info("rc_contract_0001")
        assert info is not None and (info.dim, info.count) == (other_dim, 0)

    # ---- queries ------------------------------------------------------------------------------

    def test_query_ordering_per_metric(self, store: VectorStore) -> None:
        for i, metric in enumerate(self.metrics(store)):
            name = f"rc_metric_{i:04d}"
            records = self._loaded(store, metric, name=name)
            rng = random.Random(SEED + i)
            for _ in range(10):
                q = random_vector(rng, DIM)
                got = store.query(name, q, 10)
                self._check(store, got, records, q, 10, metric, None)

    def test_k_larger_than_collection(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        records = self._loaded(store, metric)
        q = random_vector(random.Random(5), DIM)
        got = store.query("rc_contract_0001", q, N_RECORDS + 50)
        assert len(got) == N_RECORDS
        self._check(store, got, records, q, N_RECORDS + 50, metric, None)

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
                self._check(store, got, records, q, 10, metric, f)

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
            self._check(store, got, records, q, 10, metric, f)

    def test_selective_filter_returns_min_k_matching(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        rng = random.Random(SEED)
        records = make_records(rng)
        # About 2% of records carry a rare value.
        rare = [i for i in range(0, N_RECORDS, 50)]
        records = [
            Record(r.id, r.vector, {**r.metadata, "lang": "rare"}) if i in rare else r for i, r in enumerate(records)
        ]
        self._ensure(store, self.spec(metric=metric))
        self._put(store, "rc_contract_0001", records)
        f = parse_filter({"lang": "rare"}, full_schema())
        for k in (2, 10):
            q = random_vector(rng, DIM)
            got = store.query("rc_contract_0001", q, k, f)
            if store.capabilities.filter_exact:
                assert len(got) == min(k, len(rare))
            self._check(store, got, records, q, k, metric, f)

    def test_concurrent_upserts_and_queries(self, store: VectorStore) -> None:
        metric = self.metrics(store)[0]
        self._ensure(store, self.spec(metric=metric))
        errors: list[BaseException] = []

        def work(seed: int) -> None:
            try:
                rng = random.Random(seed)
                for _ in range(10):
                    store.upsert("rc_contract_0001", make_records(rng, 10))
                    self.settle(store, "rc_contract_0001")
                    store.query("rc_contract_0001", random_vector(rng, DIM), 5,
                                parse_filter({"public": True}, full_schema()))  # fmt: skip
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert all(not t.is_alive() for t in threads), "worker threads did not finish"
        assert not errors, errors
        self.settle(store, "rc_contract_0001")
        info = store.collection_info("rc_contract_0001")
        assert info is not None and info.count == 400


class CollectionOptionsContract:
    """Mixin for adapters with immutable index options (``VectorStoreType.immutable_fields``).

    A collection records the options it was built with. Opening the same storage with different
    options and ensuring the same collection must raise ``CollectionMismatch``; the same options
    must be accepted. Provide two differing option sets and a way to open a store on shared
    storage (a directory under ``tmp_path``, or a fresh schema or prefix on a live server)::

        class TestMyOptions(CollectionOptionsContract):
            options_a = {"quantization": "none"}
            options_b = {"quantization": "scalar"}

            def open_store(self, options, tmp_path):
                return MyStore(path=tmp_path / "db", **options)
    """

    options_a: ClassVar[dict[str, Any]] = {}
    options_b: ClassVar[dict[str, Any]] = {}

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> VectorStore:  # pragma: no cover - overridden
        raise NotImplementedError("return a store configured with `options`, sharing storage across calls")

    @staticmethod
    def _spec(store: VectorStore) -> CollectionSpec:
        return CollectionSpec("rc_options_0001", DIM, sorted(store.capabilities.metrics)[0], dict(SCHEMA_FIELDS))

    def test_option_sets_differ(self) -> None:
        assert self.options_a != self.options_b, "options_a and options_b must differ"

    def settle(self, store: VectorStore, name: str) -> None:
        """Override for eventual stores, as in VectorStoreContract."""

    def _ensure_options(self, store: VectorStore) -> None:
        store.ensure_collection(self._spec(store))
        self.settle(store, "rc_options_0001")

    def test_changed_options_raise_collection_mismatch(self, tmp_path: Path) -> None:
        a = self.open_store(dict(self.options_a), tmp_path)
        try:
            self._ensure_options(a)
        finally:
            a.close()
        # Close before reopening: embedded databases may hold an exclusive directory lock.
        b = self.open_store(dict(self.options_b), tmp_path)
        try:
            with pytest.raises(CollectionMismatch):
                b.ensure_collection(self._spec(b))
        finally:
            b.close()
        again = self.open_store(dict(self.options_a), tmp_path)
        try:
            self._ensure_options(again)
            assert again.collection_info("rc_options_0001") is not None
        finally:
            again.close()

    def test_same_options_reopen_without_error(self, tmp_path: Path) -> None:
        a = self.open_store(dict(self.options_a), tmp_path)
        try:
            self._ensure_options(a)
        finally:
            a.close()
        again = self.open_store(dict(self.options_a), tmp_path)
        try:
            self._ensure_options(again)
            info = again.collection_info("rc_options_0001")
            assert info is not None and info.dim == DIM
        finally:
            again.close()
