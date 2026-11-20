"""Phase V2: live checking (``stores/check.py``, the deep provider test, and ``ragcentre store check``)."""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ragcentre import cli
from ragcentre.cli import app
from ragcentre.context import AppContext
from ragcentre.models.filters import Filter, parse_filter
from ragcentre.stores.base import Capabilities, CollectionInfo, CollectionSpec, Match, Record, StoreError
from ragcentre.stores.check import CheckReport, _verify_query, check_store
from ragcentre.stores.sqlite_vec import SqliteVecStore


def temp_names(report: object) -> list[str]:
    r = report.as_dict() if hasattr(report, "as_dict") else report
    return [r["collection"], *(f"{r['collection']}_{m}" for m in r["metrics"][1:])]


def test_check_passes_clean_on_sqlite_vec(tmp_path: Path) -> None:
    store = SqliteVecStore(tmp_path / "vectors.db")
    try:
        report = check_store(store, store_type="sqlite_vec")
        assert report.ok, report.as_dict()
        assert report.records == 50
        assert report.metrics == ["cosine", "l2"]
        assert not any(f.level != "info" for f in report.findings), report.as_dict()
        checks = {f.check for f in report.findings}
        assert {"healthcheck", "create", "upsert", "count", "delete", "drop"} <= checks
        assert any(f.check == "filter:selective" for f in report.findings)
        assert {
            "filter:$eq",
            "filter:$ne",
            "filter:$in",
            "filter:$nin",
            "filter:$gt",
            "filter:$gte",
            "filter:$lt",
            "filter:$lte",
        } <= checks
        # the temporary collection is gone
        for name in temp_names(report):
            assert store.collection_info(name) is None, name
    finally:
        store.close()


def test_finding_timings_are_milliseconds(tmp_path: Path) -> None:
    import time

    class SlowUpsert(SqliteVecStore):
        def upsert(self, name: str, records: object) -> None:
            time.sleep(0.05)
            super().upsert(name, records)  # type: ignore[arg-type]

    store = SlowUpsert(tmp_path / "vectors.db")
    try:
        report = check_store(store, store_type="sqlite_vec")
        upsert = next(f for f in report.findings if f.check == "upsert")
        assert 40 <= upsert.ms < 5000, upsert.ms  # seconds mislabelled as ms would be ~0.05
        assert report.as_dict()["findings"][0]["ms"] >= 0
    finally:
        store.close()


def test_eventual_store_is_not_polled_before_the_first_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from ragcentre.stores import check as check_module

    class Eventual(SqliteVecStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.capabilities = Capabilities(
                metrics=frozenset({"cosine", "l2"}), max_batch=1000, consistency="eventual"
            )

    monkeypatch.setattr(check_module, "SETTLE_TIMEOUT", 2.0)
    store = Eventual(tmp_path / "vectors.db")
    try:
        t0 = time.monotonic()
        report = check_store(store, store_type="sqlite_vec")
        elapsed = time.monotonic() - t0
        assert report.ok, report.as_dict()
        # Waiting for 50 records before writing any would cost the full 2 s per metric.
        assert elapsed < 1.5, elapsed
    finally:
        store.close()


def test_check_passes_clean_on_qdrant_memory() -> None:
    pytest.importorskip("qdrant_client")
    from ragcentre.stores.qdrant import QdrantStore

    store = QdrantStore.connect(path=":memory:")
    try:
        report = check_store(store, store_type="qdrant")
        assert report.ok, report.as_dict()
        assert report.metrics == ["cosine", "dot", "l2"]
        assert not any(f.level != "info" for f in report.findings), report.as_dict()
        for name in temp_names(report):
            assert store.collection_info(name) is None, name
    finally:
        store.close()


def test_deep_provider_test_runs_the_store_check(ctx: AppContext, client: TestClient) -> None:
    ctx.providers.create("vs", "vector_store", "sqlite_vec", {})
    provider_id = ctx.providers.by_name("vs").id
    shallow = client.post(f"/v1/providers/{provider_id}/test").json()
    assert shallow["ok"] is True and "check" not in shallow  # the shallow response is unchanged
    deep = client.post(f"/v1/providers/{provider_id}/test", params={"deep": "true"}).json()
    assert deep["ok"] is True
    assert deep["check"]["ok"] is True
    assert deep["check"]["store_type"] == "sqlite_vec"
    assert deep["check"]["records"] == 50
    assert deep["check"]["findings"]


def test_a_forced_failure_is_a_readable_finding_and_the_collection_is_dropped() -> None:
    class BrokenStore:
        capabilities = Capabilities(metrics=frozenset({"cosine"}), max_batch=1000)

        def __init__(self) -> None:
            self.created: set[str] = set()

        def healthcheck(self) -> None:
            return None

        def ensure_collection(self, spec: object) -> None:
            self.created.add(spec.name)  # type: ignore[attr-defined]

        def collection_info(self, name: str) -> CollectionInfo | None:
            return CollectionInfo(8, "cosine", 0) if name in self.created else None

        def drop_collection(self, name: str) -> None:
            self.created.discard(name)

        def upsert(self, name: str, records: object) -> None:
            raise StoreError("simulated write failure")

        def delete(self, name: str, ids: object) -> None:
            return None

        def query(self, name: str, vector: object, k: int, filter: object = None) -> list[object]:
            return []

        def close(self) -> None:
            return None

    store = BrokenStore()
    report = check_store(store, store_type="broken")
    assert not report.ok
    assert any(f.level == "error" and f.check == "upsert" for f in report.findings), report.as_dict()
    assert store.created == set()  # the temporary collection was dropped even on failure


def test_cli_direct_store_check(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["store", "check", "--type", "sqlite_vec", "--config", json.dumps({"path": str(tmp_path / "v.db")})]
    )
    assert result.exit_code == 0, result.output
    assert "usable" in result.output
    assert "all checks passed" in result.output


def test_cli_direct_store_check_json_output(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--json", "store", "check", "--type", "sqlite_vec", "--config", json.dumps({"path": str(tmp_path / "v.db")})],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["ok"] is True and body["store_type"] == "sqlite_vec" and body["findings"]


def test_cli_direct_store_check_requires_type_or_provider() -> None:
    result = CliRunner().invoke(app, ["store", "check"])
    assert result.exit_code == 1
    assert "give either a provider name" in result.output


def test_cli_store_check_by_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubClient:
        def provider(self, ref: str) -> dict[str, object]:
            return {"id": "p1", "name": ref, "kind": "vector_store"}

        def post(self, path: str, json: object = None, **kwargs: object) -> dict[str, object]:
            assert kwargs.get("params") == {"deep": "true"}
            return {
                "ok": True,
                "latency_ms": 3.0,
                "message": None,
                "service": None,
                "dimensions": None,
                "version_drift": None,
                "check": {
                    "ok": True,
                    "store_type": "sqlite_vec",
                    "collection": "rc_check_x",
                    "records": 50,
                    "metrics": ["cosine"],
                    "seconds": 0.1,
                    "findings": [],
                },
            }

        def close(self) -> None:
            return None

    stub = StubClient()
    monkeypatch.setattr(cli.state, "client_factory", lambda: stub)
    result = CliRunner().invoke(app, ["store", "check", "my-store"])
    assert result.exit_code == 0, result.output
    assert "usable" in result.output


def test_store_secrets_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGCENTRE_QDRANT_API_KEY", "from-env")
    monkeypatch.delenv("RAGCENTRE_QDRANT_OTHER", raising=False)
    assert cli._store_env_secrets("qdrant", ["api_key", "other"]) == {"api_key": "from-env"}


def test_cli_rejects_unknown_type_and_bad_config() -> None:
    unknown = CliRunner().invoke(app, ["store", "check", "--type", "nope"])
    assert unknown.exit_code == 1 and "unknown store type" in unknown.output
    bad = CliRunner().invoke(app, ["store", "check", "--type", "sqlite_vec", "--config", "{not json"])
    assert bad.exit_code == 1 and "not valid JSON" in bad.output


def test_cli_exits_1_when_the_store_is_not_usable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RAGCENTRE_DATA_DIR", str(tmp_path))
    from ragcentre.stores import check as check_mod

    def failing(store: object, **kwargs: object) -> check_mod.CheckReport:
        report = check_mod.CheckReport(store_type="sqlite_vec")
        report.add("error", "upsert", "simulated write failure")
        return report

    monkeypatch.setattr(check_mod, "check_store", failing)
    result = CliRunner().invoke(app, ["store", "check", "--type", "sqlite_vec", "--config", "{}"])
    assert result.exit_code == 1, result.output
    assert "NOT usable" in result.output and "simulated write failure" in result.output


@pytest.mark.parametrize("filter_exact", [True, False])
@pytest.mark.parametrize("fault", ["underfill", "empty", "duplicates"])
def test_filtered_query_cardinality_is_enforced(tmp_path: Path, filter_exact: bool, fault: str) -> None:
    class FaultyQuery(SqliteVecStore):
        def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
            got = super().query(name, vector, k, filter)
            if filter is None or not got:
                return got
            if fault == "empty":
                return []
            if fault == "underfill":
                return got[:-1]
            return [got[0]] * len(got)

    store = FaultyQuery(tmp_path / "vectors.db")
    store.capabilities = replace(store.capabilities, filter_exact=filter_exact)
    try:
        report = check_store(store)
        findings = [f for f in report.findings if f.check.startswith("filter:")]
        assert findings
        if filter_exact or fault == "duplicates":
            assert not report.ok, report.as_dict()
            assert all(f.level == "error" for f in findings), report.as_dict()
            message = "duplicate record ids" if fault == "duplicates" else "unique filtered results, expected"
            assert all(message in f.message for f in findings)
        else:
            assert report.ok, report.as_dict()
            assert all(f.level in {"info", "warn"} and "filter_exact=False" in f.message for f in findings)
            assert next(f for f in findings if f.check == "filter:selective").level == "warn"
        for name in temp_names(report):
            assert store.collection_info(name) is None
    finally:
        store.close()


@pytest.mark.parametrize("filtered", [False, True])
def test_top_k_warning_reports_actual_id_overlap(tmp_path: Path, filtered: bool) -> None:
    records = [Record(id=f"{i:02}", vector=[1.0, 0.0], metadata={"lang": "en"}) for i in range(20)]

    class WrongRanking(SqliteVecStore):
        def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
            return [Match(r.id, 0.0) for r in records[-k:]]

    store = WrongRanking(tmp_path / "vectors.db")
    try:
        report = CheckReport("wrong_ranking")
        f = parse_filter({"lang": "en"}, {"lang": "str"}) if filtered else None
        _verify_query(store, report, "query:cosine", "unused", "cosine", records, random.Random(0), f, lost="ranking")
        assert report.ok
        finding = report.findings[0]
        assert finding.level == "warn"
        assert "0 of 10 expected ids overlap" in finding.message
    finally:
        store.close()


@pytest.mark.parametrize("failure", [None, "create", "upsert"])
def test_metrics_release_their_collection_before_the_next_metric(tmp_path: Path, failure: str | None) -> None:
    class LimitedStore(SqliteVecStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.capabilities = replace(self.capabilities, max_collections=1, max_batch=2)
            self.live: set[str] = set()
            self.created: list[str] = []
            self.dropped: list[str] = []

        def ensure_collection(self, spec: CollectionSpec) -> None:
            if self.live:
                raise StoreError("collection limit exceeded")
            super().ensure_collection(spec)
            self.live.add(spec.name)
            self.created.append(spec.name)
            if failure == "create" and len(self.created) == 1:
                raise StoreError("failed after creating the collection")

        def upsert(self, name: str, records: Sequence[Record]) -> None:
            assert len(records) <= self.capabilities.max_batch
            if failure == "upsert" and name == self.created[0]:
                raise StoreError("write failed")
            super().upsert(name, records)

        def delete(self, name: str, ids: Sequence[str]) -> None:
            assert len(ids) <= self.capabilities.max_batch
            super().delete(name, ids)

        def drop_collection(self, name: str) -> None:
            super().drop_collection(name)
            self.live.remove(name)
            self.dropped.append(name)

    store = LimitedStore(tmp_path / "vectors.db")
    try:
        report = check_store(store)
        assert report.ok == (failure is None), report.as_dict()
        assert len(store.created) == len(report.metrics) == 2
        assert store.created == store.dropped
        assert not store.live
        assert any(f.check == "query:l2" and f.level == "info" for f in report.findings)
        for name in store.created:
            assert store.collection_info(name) is None
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["parser", "embedder", "chunker", "reranker"])
def test_cli_store_check_rejects_other_provider_kinds_before_testing(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    class StubClient:
        def provider(self, ref: str) -> dict[str, object]:
            return {"id": "p1", "name": ref, "kind": kind}

        def post(self, *args: object, **kwargs: object) -> None:
            pytest.fail("non-store providers must be rejected before POST /test")

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli.state, "client_factory", StubClient)
    result = CliRunner().invoke(app, ["store", "check", "my-provider"])
    assert result.exit_code == 1, result.output
    assert "provider 'my-provider' is not a vector_store" in result.output


def test_cli_provider_deep_help_mentions_vector_stores() -> None:
    result = CliRunner().invoke(app, ["provider", "test", "--help"], color=False)
    assert result.exit_code == 0, result.output
    assert "for a vector" in result.output
    assert "store: check live data" in result.output
