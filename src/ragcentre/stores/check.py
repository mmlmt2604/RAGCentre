"""Live checking for vector stores (Phase V2, G8).

``ragcentre store check`` and ``POST /v1/providers/{id}/test?deep=true`` run this. It depends on
nothing but the ``VectorStore`` interface, the filter evaluator and seeded random data, so it works
for any adapter — Python or HTTP shim — without importing a vendor client.

The check creates a temporary collection, writes 50 records, settles, and queries it under every
metric the store supports, one filter per operator, and a selective filter, then deletes and drops.
Each step yields a finding (error, warn or info) that names the capability it proves, with its
timing. The temporary collection is always dropped, even when a step fails.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from ragcentre.models.filters import FieldType, Filter, matches, parse_filter
from ragcentre.stores.base import CollectionSpec, Metric, Record, VectorStore, delete_batched, upsert_batched
from ragcentre.testing.random_data import SCHEMA_FIELDS, full_schema, random_metadata, random_vector
from ragcentre.vectors import cosine_similarity, dot, float32, l2_distance

Level = Literal["error", "warn", "info"]

DIM = 8
N_RECORDS = 50
SEED = 20240611
TOL = 1e-3  # looser than the contract suite: the check proves capability, not exact float equality
MIN_RECALL = 0.9
SETTLE_TIMEOUT = 10.0

# One filter per operator, on a field whose type admits it (the range operators need a number).
_OPERATORS: list[tuple[str, str, Any]] = [
    ("$eq", "lang", "en"),
    ("$ne", "lang", "de"),
    ("$gt", "year", 2018),
    ("$gte", "score", 0.5),
    ("$lt", "year", 2023),
    ("$lte", "score", 0.5),
    ("$in", "lang", ["en", "fr"]),
    ("$nin", "lang", ["ja", "zz"]),
]


@dataclass
class Finding:
    level: Level
    check: str
    message: str
    ms: float = 0.0
    lost: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "check": self.check,
            "message": self.message,
            "ms": round(self.ms, 2),
            "lost": self.lost,
        }


@dataclass
class CheckReport:
    store_type: str
    findings: list[Finding] = field(default_factory=list)
    collection: str = ""
    records: int = 0
    metrics: list[Metric] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.findings)

    def add(self, level: Level, check: str, message: str, seconds: float = 0.0, lost: str | None = None) -> None:
        """Record a finding. Callers pass elapsed seconds; findings store milliseconds."""
        self.findings.append(Finding(level, check, message, seconds * 1000.0, lost))

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "store_type": self.store_type,
            "collection": self.collection,
            "records": self.records,
            "metrics": self.metrics,
            "seconds": round(self.seconds, 2),
            "findings": [f.as_dict() for f in self.findings],
        }


def _metadata(rng: random.Random) -> dict[str, str | int | float | bool]:
    # random_metadata returns scalars only; the Record type needs them narrowed from Any.
    return cast("dict[str, str | int | float | bool]", random_metadata(rng))


def _make_records(rng: random.Random, n: int, dim: int) -> list[Record]:
    records: list[Record] = []
    for i in range(n):
        meta = _metadata(rng)
        if i % 10 == 0:
            meta["lang"] = "rare"  # a selective filter later matches only these
        records.append(
            Record(
                id=str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                vector=float32(random_vector(rng, dim)),
                metadata=meta,
            )
        )
    return records


def _brute_score(metric: Metric, a: Sequence[float], b: Sequence[float]) -> float:
    if metric == "cosine":
        return cosine_similarity(a, b)
    if metric == "l2":
        return -l2_distance(a, b)
    return dot(a, b)


def _expected(
    records: Sequence[Record], q: Sequence[float], k: int, metric: Metric, f: Filter | None
) -> list[tuple[str, float]]:
    scored = [(r.id, _brute_score(metric, r.vector, q)) for r in records if matches(f, r.metadata)]
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:k]


def check_store(
    store: VectorStore, *, store_type: str | None = None, dim: int = DIM, n: int = N_RECORDS
) -> CheckReport:
    """Run a contract-lite check against ``store`` and return a ``CheckReport``.

    The temporary collection is dropped on every path (including failure). ``store_type`` labels the
    report; when omitted, the adapter class name is used."""
    report = CheckReport(store_type=store_type or type(store).__name__)
    started = time.monotonic()
    rng = random.Random(SEED)
    records = _make_records(rng, n, dim)
    report.records = len(records)
    report.metrics = sorted(store.capabilities.metrics)
    name = f"rc_check_{uuid.uuid4().hex[:8]}"
    report.collection = name
    try:
        if not _run(report, "healthcheck", "reachable: yes", store.healthcheck):
            return report
        for i, metric in enumerate(report.metrics):
            coll = name if i == 0 else f"{name}_{metric}"
            try:
                _check_metric(store, report, coll, metric, records, rng, run_filters=(i == 0))
            finally:
                # Release each metric's collection before creating the next one, including when
                # creation partially succeeds or a later operation fails (max_collections may be 1).
                _drop_all(store, report, [coll])
    finally:
        report.seconds = time.monotonic() - started
    return report


def _run(report: CheckReport, check: str, message: str, fn: Callable[[], None]) -> bool:
    """Run one store operation, adding an info finding on success or an error finding on failure."""
    t0 = time.perf_counter()
    try:
        fn()
    except Exception as exc:
        report.add("error", check, str(exc) or type(exc).__name__, time.perf_counter() - t0)
        return False
    report.add("info", check, message, time.perf_counter() - t0)
    return True


def _settle(store: VectorStore, name: str, expected: int | None) -> None:
    """Wait until a write is visible to queries. Immediate stores need no wait; eventual stores poll
    ``collection_info`` until the count matches. A timeout is not an error: the count step reports it."""
    if expected is None or store.capabilities.consistency == "immediate":
        return
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while True:
        try:
            info = store.collection_info(name)
            if info is not None and info.count == expected:
                return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return
        time.sleep(0.05)


def _check_metric(
    store: VectorStore,
    report: CheckReport,
    name: str,
    metric: Metric,
    records: list[Record],
    rng: random.Random,
    *,
    run_filters: bool,
) -> None:
    dim = len(records[0].vector)
    spec = CollectionSpec(name=name, dim=dim, metric=metric, fields=dict(SCHEMA_FIELDS))
    if not _run(report, "create", f"collection {name} (metric {metric}): yes", lambda: store.ensure_collection(spec)):
        return
    # No settle here: nothing has been written yet, so waiting for ``len(records)`` would only
    # burn the whole timeout on an eventually consistent store.
    if not _run(report, "upsert", f"wrote {len(records)} records: yes", lambda: upsert_batched(store, name, records)):
        return
    _settle(store, name, len(records))
    _count(store, report, name, len(records))
    _verify_query(
        store, report, f"query:{metric}", name, metric, records, rng, None, lost="exact nearest-neighbour ranking"
    )
    if run_filters:
        _filters(store, report, name, metric, records, rng)
        _selective(store, report, name, metric, records, rng)
        _delete(store, report, name, records)


def _count(store: VectorStore, report: CheckReport, name: str, expected: int, check: str = "count") -> None:
    t0 = time.perf_counter()
    try:
        info = store.collection_info(name)
    except Exception as exc:
        report.add(
            "warn", check, f"collection_info failed: {exc}", time.perf_counter() - t0, lost="index coverage counts"
        )
        return
    ms = time.perf_counter() - t0
    if info is None:
        report.add(
            "warn", check, "collection_info returned None for an existing collection", ms, lost="index coverage counts"
        )
    elif info.count != expected:
        report.add("warn", check, f"count {info.count}, expected {expected}", ms, lost="index coverage counts")
    else:
        report.add("info", check, f"count {info.count}: yes", ms)


def _verify_query(
    store: VectorStore,
    report: CheckReport,
    check: str,
    name: str,
    metric: Metric,
    records: list[Record],
    rng: random.Random,
    f: Filter | None,
    *,
    lost: str | None,
    k: int = 10,
) -> None:
    """One query, checked against a brute-force computation with ``matches`` as the ground truth."""
    q = float32(random_vector(rng, len(records[0].vector)))
    t0 = time.perf_counter()
    try:
        got = store.query(name, q, k, f)
    except Exception as exc:
        report.add("error", check, f"query failed: {exc}", time.perf_counter() - t0)
        return
    ms = time.perf_counter() - t0
    by_id = {r.id: r for r in records}
    for m in got:
        if m.id not in by_id:
            report.add("error", check, f"returned unknown id {m.id}", ms)
            return
        if not matches(f, by_id[m.id].metadata):
            report.add("error", check, f"id {m.id} does not satisfy the filter", ms)
            return
    if len(got) > k:
        report.add("error", check, f"returned {len(got)} results for k={k}", ms)
        return
    got_ids = [m.id for m in got]
    if len(set(got_ids)) != len(got_ids):
        report.add("error", check, "returned duplicate record ids", ms)
        return
    want = _expected(records, q, k, metric, f)
    if f is not None and store.capabilities.filter_exact and len(got_ids) < len(want):
        report.add(
            "error",
            check,
            f"returned {len(got_ids)} unique filtered results, expected {len(want)} (filter_exact=True)",
            ms,
            lost=lost,
        )
        return
    if f is not None and not store.capabilities.filter_exact:
        want_set = {w[0] for w in want}
        recall = len({m.id for m in got} & want_set) / len(want_set) if want_set else 1.0
        if recall < MIN_RECALL:
            report.add("warn", check, f"recall {recall:.2f} below {MIN_RECALL} (filter_exact=False)", ms, lost=lost)
        else:
            report.add("info", check, f"recall {recall:.2f} against brute force (filter_exact=False): yes", ms)
        return
    want_ids = [w[0] for w in want]
    if got_ids != want_ids:
        overlap = len(set(got_ids) & set(want_ids))
        report.add(
            "warn",
            check,
            f"top-k differs from brute force ({overlap} of {len(want_ids)} expected ids overlap)",
            ms,
            lost=lost,
        )
        return
    for m, (_, want_score) in zip(got, want, strict=True):
        if abs(m.score - want_score) > TOL:
            report.add(
                "warn", check, f"score {m.score} differs from expected {want_score}", ms, lost="exact score values"
            )
            return
    report.add("info", check, f"top-k and scores match brute force (k={k}): yes", ms)


def _filters(
    store: VectorStore, report: CheckReport, name: str, metric: Metric, records: list[Record], rng: random.Random
) -> None:
    schema: dict[str, FieldType] = full_schema()
    for op, fname, value in _OPERATORS:
        f = parse_filter({fname: {op: value}}, schema)
        _verify_query(store, report, f"filter:{op}", name, metric, records, rng, f, lost="filters honour k")


def _selective(
    store: VectorStore, report: CheckReport, name: str, metric: Metric, records: list[Record], rng: random.Random
) -> None:
    schema: dict[str, FieldType] = full_schema()
    f = parse_filter({"lang": "rare"}, schema)
    rare = [r for r in records if matches(f, r.metadata)]
    # k larger than the number of matches proves the store returns min(k, matching), never more.
    _verify_query(
        store,
        report,
        "filter:selective",
        name,
        metric,
        records,
        rng,
        f,
        lost="filters honour k",
        k=max(10, len(rare) + 1),
    )


def _delete(store: VectorStore, report: CheckReport, name: str, records: list[Record]) -> None:
    doomed = [records[0].id, records[1].id, str(uuid.uuid4())]
    expected = len(records) - 2
    t0 = time.perf_counter()
    try:
        delete_batched(store, name, doomed)
        _settle(store, name, expected)
        info = store.collection_info(name)
    except Exception as exc:
        report.add("error", "delete", f"delete failed: {exc}", time.perf_counter() - t0)
        return
    ms = time.perf_counter() - t0
    if info is None or info.count != expected:
        report.add("warn", "delete", f"count after delete is {info.count if info else 'missing'}, expected {expected}", ms,
                   lost="document deletion from the index")  # fmt: skip
        return
    report.add("info", "delete", f"deleted 2 live ids (1 missing): count now {expected}: yes", ms)


def _drop_all(store: VectorStore, report: CheckReport, created: list[str]) -> None:
    for coll in reversed(created):
        t0 = time.perf_counter()
        try:
            store.drop_collection(coll)
            report.add("info", "drop", f"removed temporary collection {coll}: yes", time.perf_counter() - t0)
        except Exception as exc:
            report.add("warn", "drop", f"could not drop {coll}: {exc}", time.perf_counter() - t0,
                       lost="clean removal of temporary collections")  # fmt: skip
