"""pgvector adapter tests; they need PostgreSQL with pgvector (see tests/docker-compose.yml).

The DSN comes from ``RAGCENTRE_TEST_PGVECTOR_DSN`` (default: the compose service). Each test uses
its own schema, dropped afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("psycopg")

from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, Record, StoreError, VectorStore
from ragcentre.stores.pgvector import HNSW_MAX_DIM, PgvectorStore, vector_literal
from ragcentre.stores.registry import vector_store_types
from ragcentre.testing.vector_store_contract import VectorStoreContract

pytestmark = pytest.mark.integration

DSN = os.environ.get("RAGCENTRE_TEST_PGVECTOR_DSN", "postgresql://ragcentre:ragcentre@127.0.0.1:54329/ragcentre")


def _reachable() -> str | None:
    import psycopg

    try:
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        return str(exc).strip().splitlines()[0]
    return None


@pytest.fixture(scope="module")
def dsn() -> str:
    problem = _reachable()
    if problem is not None:
        pytest.skip(f"PostgreSQL with pgvector is not reachable at the test DSN ({problem})")
    return DSN


def _drop_schema(schema: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def fresh(dsn: str) -> Iterator[Any]:
    created: list[PgvectorStore] = []

    def make() -> PgvectorStore:
        s = PgvectorStore(dsn, schema=f"rc_test_{uuid.uuid4().hex[:12]}")
        created.append(s)
        return s

    yield make
    for s in created:
        s.close()
        _drop_schema(s.schema)


class TestPgvectorContract(VectorStoreContract):
    @pytest.fixture
    def store(self, fresh: Any) -> PgvectorStore:
        return fresh()  # type: ignore[no-any-return]


def test_is_a_vector_store(fresh: Any) -> None:
    s = fresh()
    assert isinstance(s, VectorStore)
    assert s.capabilities.metrics == frozenset({"cosine", "l2", "dot"})
    assert s.capabilities.max_batch == 500


def test_missing_collection(fresh: Any) -> None:
    s = fresh()
    with pytest.raises(CollectionNotFound):
        s.query("rc_missing_0001", [1.0, 0.0], 3)
    s.delete("rc_missing_0001", [str(uuid.uuid4())])


def test_indexes_and_large_dimension(fresh: Any) -> None:
    import psycopg

    s = fresh()
    s.ensure_collection(CollectionSpec(name="rc_small_0001", dim=4, metric="dot"))
    s.ensure_collection(CollectionSpec(name="rc_large_0001", dim=HNSW_MAX_DIM + 1, metric="cosine"))
    with psycopg.connect(DSN) as conn:
        rows = conn.execute(
            "SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = %s ORDER BY tablename, indexdef",
            (s.schema,),
        ).fetchall()
    defs = {(t, d.split(" USING ")[1].split(" ")[0]) for t, d in rows}
    assert ("rc_small_0001", "hnsw") in defs and ("rc_small_0001", "gin") in defs
    assert ("rc_large_0001", "gin") in defs and ("rc_large_0001", "hnsw") not in defs
    assert any("vector_ip_ops" in d for t, d in rows if t == "rc_small_0001")
    s.upsert("rc_large_0001", [Record(str(uuid.uuid4()), [1.0] * (HNSW_MAX_DIM + 1), {})])
    assert s.query("rc_large_0001", [1.0] * (HNSW_MAX_DIM + 1), 1)[0].score == pytest.approx(1.0)


def test_selective_filter_with_index_scan(dsn: str) -> None:
    """Through the HNSW index (sequential scans disabled), a 1% filter still returns k rows.

    A plain HNSW scan returns at most ``ef_search`` candidates before filtering, so here it finds
    almost none; the store's iterative scan must keep going until ``k`` rows pass.
    """
    import psycopg
    from psycopg.conninfo import make_conninfo

    forced = make_conninfo(dsn, options="-c enable_seqscan=off")
    s = PgvectorStore(forced, schema=f"rc_test_{uuid.uuid4().hex[:12]}")
    try:
        _selective(s, forced, psycopg)
    finally:
        s.close()
        _drop_schema(s.schema)


def _selective(s: PgvectorStore, forced: str, psycopg: Any) -> None:
    import random

    if not s._iterative_scan:
        pytest.skip(f"pgvector {s.extension_version} has no iterative index scans")
    s.ensure_collection(CollectionSpec(name="rc_sel_00001", dim=8, metric="l2", fields={"lang": "str"}))
    rng = random.Random(7)
    records = [
        Record(str(uuid.uuid4()), [rng.uniform(-1, 1) for _ in range(8)], {"lang": "rare" if i % 100 == 0 else "en"})
        for i in range(5000)
    ]
    for i in range(0, len(records), 500):
        s.upsert("rc_sel_00001", records[i : i + 500])
    table = f'"{s.schema}".rc_sel_00001'
    plain = (
        f"SELECT id FROM {table} WHERE (metadata->>'lang') = 'rare' ORDER BY embedding <-> '[0,0,0,0,0,0,0,0]' LIMIT 40"
    )
    with psycopg.connect(forced) as conn:
        plan = "\n".join(r[0] for r in conn.execute(f"EXPLAIN {plain}").fetchall())
        assert "Index Scan" in plan and "embedding_idx" in plan, plan
        assert len(conn.execute(plain).fetchall()) < 40  # the problem the iterative scan solves
    got = s.query("rc_sel_00001", [0.0] * 8, 40, parse_filter({"lang": "rare"}, {"lang": "str", "document_id": "str"}))
    assert len(got) == 40
    assert [m.score for m in got] == sorted((m.score for m in got), reverse=True)


def test_rejects_bad_schema_and_missing_dsn(dsn: str, tmp_path: Any) -> None:
    with pytest.raises(StoreError):
        PgvectorStore(dsn, schema="Bad;Schema")
    t = vector_store_types()["pgvector"]
    with pytest.raises(StoreError, match="dsn"):
        t.factory(t.config_model.model_validate({}), {}, tmp_path)


def test_vector_literal() -> None:
    assert vector_literal([1, 0.5, -2.25]) == "[1.0,0.5,-2.25]"
