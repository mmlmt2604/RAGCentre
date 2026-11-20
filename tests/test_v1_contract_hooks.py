"""The V1 contract-suite hooks, shown on a configurable in-memory store.

The ``Test*`` classes below run the real contract suite against stores that declare different
capabilities. The ``test_*`` functions show that the suite fails when a store breaks a promise.
"""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import METRICS, Capabilities, Match, VectorStore, upsert_batched
from ragcentre.stores.catalog import InStoreCatalog, LocalCatalog
from ragcentre.testing.random_data import full_schema, random_vector
from ragcentre.testing.vector_store_contract import (
    DIM,
    MIN_RECALL,
    TOL,
    CollectionOptionsContract,
    VectorStoreContract,
    assert_recall,
    assert_same_results,
    make_records,
    wait_until,
)
from vs_support import MemoryStore

CAPS = Capabilities(metrics=frozenset(METRICS), max_batch=50)


def in_store_catalog() -> InStoreCatalog:
    texts: dict[str, str] = {}

    def remove(name: str) -> None:
        texts.pop(name, None)

    return InStoreCatalog(texts.get, texts.__setitem__, remove)


# ---- the suite itself, against stores with different promises ---------------------------------


class TestImmediateExactStore(VectorStoreContract):
    @pytest.fixture
    def store(self) -> MemoryStore:
        return MemoryStore(in_store_catalog(), capabilities=CAPS)


class TestEventualStoreWithSettle(VectorStoreContract):
    """Writes become visible 30 ms late; the suite must call ``settle`` after every write."""

    @pytest.fixture
    def store(self) -> MemoryStore:
        caps = replace(CAPS, consistency="eventual")
        return MemoryStore(in_store_catalog(), capabilities=caps, delay=0.03)

    def settle(self, store: Any, name: str) -> None:
        wait_until(lambda: store.pending() == 0, timeout=5, what="writes to become visible")


class TestMaxDimStore(VectorStoreContract):
    @pytest.fixture
    def store(self) -> MemoryStore:
        return MemoryStore(in_store_catalog(), capabilities=replace(CAPS, max_dim=DIM))

    def test_limit_is_a_real_limit(self, store: MemoryStore) -> None:
        store.ensure_collection(self.spec(dim=DIM))  # exactly at the limit is fine


class TestFilterRecallStore(VectorStoreContract):
    """``filter_exact=False``: filtered queries are checked by recall. This store loses its 10th
    ranked result on every filtered query (recall 0.9), which the exact check would reject."""

    pytestmark = pytest.mark.filterwarnings("ignore:filter_exact=False")

    @pytest.fixture
    def store(self) -> MemoryStore:
        return MemoryStore(in_store_catalog(), capabilities=replace(CAPS, filter_exact=False), drop_every=10)

    def test_the_report_names_the_mode(self, request: pytest.FixtureRequest, store: MemoryStore) -> None:
        assert ("filter_mode", f"recall>={MIN_RECALL}") in request.node.user_properties


class TestWideTolerance(VectorStoreContract):
    """A store whose scores are off by 3e-4 passes once its adapter declares a wider tolerance."""

    score_tolerance = 1e-3

    @pytest.fixture
    def store(self) -> MemoryStore:
        return MemoryStore(in_store_catalog(), capabilities=CAPS, score_offset=3e-4)


class TestOptionsMixin(CollectionOptionsContract):
    options_a: ClassVar[dict[str, Any]] = {"quantization": "none"}
    options_b: ClassVar[dict[str, Any]] = {"quantization": "scalar"}

    def open_store(self, options: dict[str, Any], tmp_path: Path) -> VectorStore:
        return MemoryStore(LocalCatalog(tmp_path, "mem_options", "one"), capabilities=CAPS, options=options)


# ---- the suite fails when a promise is broken -------------------------------------------------

FAILED = pytest.fail.Exception  # what a failing check inside a called test method raises


def _run(contract: type[VectorStoreContract], test: str, store: Any, *args: Any) -> None:
    getattr(contract(), test)(store, *args)


def test_default_tolerance_rejects_a_scores_offset() -> None:
    store = MemoryStore(in_store_catalog(), capabilities=CAPS, score_offset=3e-4)
    with pytest.raises(AssertionError):
        _run(VectorStoreContract, "test_query_ordering_per_metric", store)
    _run(TestWideTolerance, "test_query_ordering_per_metric", store)


def test_eventual_store_fails_without_settle() -> None:
    caps = replace(CAPS, consistency="eventual")
    store = MemoryStore(in_store_catalog(), capabilities=caps, delay=0.5)
    with pytest.raises(AssertionError):
        _run(VectorStoreContract, "test_upsert_overwrite_and_count", store)


def test_exact_store_rejects_a_lossy_filtered_query() -> None:
    """The same lossy store fails the exact check, so exactness really is the default."""
    store = MemoryStore(in_store_catalog(), capabilities=CAPS, drop_every=10)
    with pytest.raises(AssertionError):
        _run(VectorStoreContract, "test_combinators", store, "random")
    # ... and unfiltered queries stay exact even for a filter_exact=False store.
    loose = MemoryStore(in_store_catalog(), capabilities=replace(CAPS, filter_exact=False), drop_every=1)
    _run(VectorStoreContract, "test_query_ordering_per_metric", loose)


def test_recall_check_thresholds_and_invariants() -> None:
    rng = random.Random(7)
    records = make_records(rng, 60)
    f = parse_filter({"public": True}, full_schema())
    q = random_vector(rng, DIM)
    store = MemoryStore(in_store_catalog(), capabilities=CAPS)
    store.ensure_collection(TestImmediateExactStore.spec(metric="cosine"))
    upsert_batched(store, "rc_contract_0001", records)
    full = store.query("rc_contract_0001", q, 10, f)
    assert len(full) == 10

    assert_recall(full, records, q, 10, "cosine", f)
    assert_recall(full[:9], records, q, 10, "cosine", f)  # recall 0.9
    with pytest.raises(AssertionError, match=r"recall 0\.80"):
        assert_recall(full[:8], records, q, 10, "cosine", f)
    with pytest.raises(AssertionError, match="duplicate"):
        assert_recall([*full[:9], full[0]], records, q, 10, "cosine", f)
    with pytest.raises(AssertionError, match="at most 10"):
        assert_recall([*full, full[0]], records, q, 10, "cosine", f)
    with pytest.raises(AssertionError, match="sorted"):
        assert_recall(list(reversed(full)), records, q, 10, "cosine", f)
    # A wrong score is wrong even when recall is perfect.
    with pytest.raises(AssertionError):
        assert_recall([Match(m.id, m.score + 0.1) for m in full], records, q, 10, "cosine", f)
    # An id that fails the filter is never acceptable.
    other = next(r for r in records if not r.metadata["public"])
    with pytest.raises(AssertionError, match="does not satisfy the filter"):
        assert_recall([*full[:9], Match(other.id, 0.0)], records, q, 10, "cosine", f)
    # The threshold cannot be set below 0.9, and the exact check is stricter than the recall check.
    with pytest.raises(AssertionError, match=r"below 0\.9"):
        assert_recall(full, records, q, 10, "cosine", f, min_recall=0.5)
    with pytest.raises(AssertionError):
        assert_same_results(full[:9], records, q, 10, "cosine", f)
    # An empty filter result passes only when nothing matches.
    none = parse_filter({"lang": "zz"}, full_schema())
    assert_recall([], records, q, 10, "cosine", none)
    assert TOL == 1e-4


def test_max_dim_test_fails_for_a_store_that_does_not_enforce_it() -> None:
    caps = replace(CAPS, max_dim=DIM)
    enforcing = MemoryStore(in_store_catalog(), capabilities=caps)
    _run(VectorStoreContract, "test_max_dim_is_enforced", enforcing)
    cheating = MemoryStore(in_store_catalog(), capabilities=caps, enforce_max_dim=False)
    with pytest.raises(FAILED):
        _run(VectorStoreContract, "test_max_dim_is_enforced", cheating)
    unlimited = MemoryStore(in_store_catalog(), capabilities=CAPS)
    with pytest.raises(pytest.skip.Exception):
        _run(VectorStoreContract, "test_max_dim_is_enforced", unlimited)


def test_options_mixin_fails_for_a_store_that_ignores_options(tmp_path: Path) -> None:
    class Ignoring(TestOptionsMixin):
        def open_store(self, options: dict[str, Any], tmp_path: Path) -> VectorStore:
            return MemoryStore(LocalCatalog(tmp_path, "mem_options", "one"), capabilities=CAPS)  # no options

    with pytest.raises(FAILED):
        Ignoring().test_changed_options_raise_collection_mismatch(tmp_path)
    Ignoring().test_same_options_reopen_without_error(tmp_path)


def test_options_mixin_needs_distinct_option_sets() -> None:
    class Same(TestOptionsMixin):
        options_b: ClassVar[dict[str, Any]] = {"quantization": "none"}

    with pytest.raises(AssertionError, match="must differ"):
        Same().test_option_sets_differ()


def test_wait_until() -> None:
    state = {"n": 0}

    def ready() -> bool:
        state["n"] += 1
        return state["n"] >= 3

    wait_until(ready, interval=0.001)
    with pytest.raises(TimeoutError, match="writes"):
        wait_until(lambda: False, timeout=0.05, interval=0.01, what="writes")
