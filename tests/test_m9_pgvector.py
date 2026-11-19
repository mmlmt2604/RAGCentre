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

from pydantic import ValidationError

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import parse_filter
from ragcentre.stores.base import (
    CollectionNotFound,
    CollectionSpec,
    DimensionLimitExceeded,
    Record,
    StoreError,
    VectorStore,
)
from ragcentre.stores.pgvector import (
    HALFVEC_HNSW_MAX_DIM,
    HNSW_MAX_DIM,
    VECTOR_MAX_DIM,
    PgvectorStore,
    vector_literal,
)
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


def _new_schema() -> str:
    return f"rc_test_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def fresh(dsn: str) -> Iterator[Any]:
    created: list[PgvectorStore] = []

    def make(**kwargs: Any) -> PgvectorStore:
        s = PgvectorStore(dsn, schema=f"rc_test_{uuid.uuid4().hex[:12]}", **kwargs)
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


class TestPgvectorHalfvecContract(VectorStoreContract):
    """The whole contract with halfvec columns; float16 storage costs ~1e-3 of score precision."""

    score_tolerance = 2e-2

    @pytest.fixture
    def store(self, fresh: Any) -> PgvectorStore:
        return fresh(vector_type="halfvec")  # type: ignore[no-any-return]


def test_is_a_vector_store(fresh: Any) -> None:
    s = fresh()
    assert isinstance(s, VectorStore)
    assert s.capabilities.metrics == frozenset({"cosine", "l2", "dot"})
    assert s.capabilities.max_batch == 500
    assert s.capabilities.max_dim == VECTOR_MAX_DIM


def test_missing_collection(fresh: Any) -> None:
    s = fresh()
    with pytest.raises(CollectionNotFound):
        s.query("rc_missing_0001", [1.0, 0.0], 3)
    s.delete("rc_missing_0001", [str(uuid.uuid4())])


def test_indexes_and_vector_type(fresh: Any) -> None:
    """auto picks vector (<=2000) or halfvec (<=4000), and drops to an exact scan above 4000."""
    import psycopg

    s = fresh()
    s.ensure_collection(CollectionSpec(name="rc_small_0001", dim=4, metric="dot"))
    s.ensure_collection(CollectionSpec(name="rc_half_0001", dim=HNSW_MAX_DIM + 1, metric="cosine"))
    s.ensure_collection(CollectionSpec(name="rc_exact_0001", dim=HALFVEC_HNSW_MAX_DIM + 1, metric="cosine"))
    with psycopg.connect(DSN) as conn:
        indexes = conn.execute(
            "SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = %s ORDER BY tablename, indexdef",
            (s.schema,),
        ).fetchall()
        cols = conn.execute(
            "SELECT table_name, udt_name FROM information_schema.columns "
            "WHERE table_schema = %s AND column_name = 'embedding' ORDER BY table_name",
            (s.schema,),
        ).fetchall()
    index_types = {(t, d.split(" USING ")[1].split(" ")[0]) for t, d in indexes}
    assert ("rc_small_0001", "hnsw") in index_types and ("rc_small_0001", "gin") in index_types
    assert ("rc_half_0001", "hnsw") in index_types and ("rc_half_0001", "gin") in index_types
    assert ("rc_exact_0001", "gin") in index_types and ("rc_exact_0001", "hnsw") not in index_types
    assert any("vector_ip_ops" in d for t, d in indexes if t == "rc_small_0001")
    assert any("halfvec_cosine_ops" in d for t, d in indexes if t == "rc_half_0001")
    assert dict(cols) == {"rc_small_0001": "vector", "rc_half_0001": "halfvec", "rc_exact_0001": "halfvec"}
    # a halfvec collection upserts and queries (self-similarity is exact despite float16)
    rec = Record(str(uuid.uuid4()), [1.0] * (HNSW_MAX_DIM + 1), {})
    s.upsert("rc_half_0001", [rec])
    assert s.query("rc_half_0001", rec.vector, 1)[0].score == pytest.approx(1.0)


def test_explicit_vector_type_falls_back_to_exact_scan(fresh: Any) -> None:
    import psycopg

    s = fresh(vector_type="vector")
    s.ensure_collection(CollectionSpec(name="rc_vec_big_0001", dim=HNSW_MAX_DIM + 1, metric="cosine"))
    with psycopg.connect(DSN) as conn:
        indexes = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = 'rc_vec_big_0001'",
            (s.schema,),
        ).fetchall()
        udt = conn.execute(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'rc_vec_big_0001' AND column_name = 'embedding'",
            (s.schema,),
        ).fetchone()
    types = {d.split(" USING ")[1].split(" ")[0] for (d,) in indexes}
    assert "gin" in types and "hnsw" not in types
    assert udt is not None and udt[0] == "vector"


def test_vector_type_recorded_and_mismatch(dsn: str) -> None:
    schema = _new_schema()
    a = PgvectorStore(dsn, schema=schema)  # auto -> vector for dim 8
    try:
        a.ensure_collection(CollectionSpec(name="rc_vt_0001", dim=8, metric="cosine"))
    finally:
        a.close()
    b = PgvectorStore(dsn, schema=schema, vector_type="halfvec")
    try:
        with pytest.raises(CollectionMismatch, match="vector type"):
            b.ensure_collection(CollectionSpec(name="rc_vt_0001", dim=8, metric="cosine"))
    finally:
        b.close()
        _drop_schema(schema)


def test_hnsw_options_recorded_and_mismatch(dsn: str) -> None:
    schema = _new_schema()
    a = PgvectorStore(dsn, schema=schema, hnsw_m=24)
    try:
        a.ensure_collection(CollectionSpec(name="rc_m_0001", dim=8, metric="cosine"))
    finally:
        a.close()
    b = PgvectorStore(dsn, schema=schema, hnsw_m=32)
    try:
        with pytest.raises(CollectionMismatch, match="hnsw"):
            b.ensure_collection(CollectionSpec(name="rc_m_0001", dim=8, metric="cosine"))
    finally:
        b.close()
    again = PgvectorStore(dsn, schema=schema, hnsw_m=24)
    try:
        again.ensure_collection(CollectionSpec(name="rc_m_0001", dim=8, metric="cosine"))
    finally:
        again.close()
    _drop_schema(schema)


def test_query_time_options_are_mutable(dsn: str) -> None:
    schema = _new_schema()
    a = PgvectorStore(dsn, schema=schema, ef_search=60, max_scan_tuples=5000)
    try:
        a.ensure_collection(CollectionSpec(name="rc_q_0001", dim=8, metric="cosine"))
    finally:
        a.close()
    b = PgvectorStore(dsn, schema=schema, ef_search=120, max_scan_tuples=9000)
    try:
        b.ensure_collection(CollectionSpec(name="rc_q_0001", dim=8, metric="cosine"))  # no mismatch
        b.upsert("rc_q_0001", [Record(str(uuid.uuid4()), [1.0] * 8, {})])
        assert b.query("rc_q_0001", [1.0] * 8, 1)[0].score == pytest.approx(1.0)
    finally:
        b.close()
        _drop_schema(schema)


def test_existing_row_without_vector_type_reads_as_vector(dsn: str) -> None:
    import psycopg
    from psycopg import sql as psql

    schema = _new_schema()
    s = PgvectorStore(dsn, schema=schema)
    try:
        s.ensure_collection(CollectionSpec(name="rc_legacy_0001", dim=8, metric="cosine"))
    finally:
        s.close()
    # Simulate a row written before vector_type / hnsw_* were recorded.
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            psql.SQL(
                "UPDATE {} SET vector_type = NULL, hnsw_m = NULL, hnsw_ef_construction = NULL WHERE name = %s"
            ).format(psql.Identifier(schema, "_collections")),
            ("rc_legacy_0001",),
        )
    s2 = PgvectorStore(dsn, schema=schema)
    try:
        s2.ensure_collection(CollectionSpec(name="rc_legacy_0001", dim=8, metric="cosine"))
        s2.upsert("rc_legacy_0001", [Record(str(uuid.uuid4()), [1.0] * 8, {})])
        assert s2.query("rc_legacy_0001", [1.0] * 8, 1)[0].score == pytest.approx(1.0)
    finally:
        s2.close()
        _drop_schema(schema)


def test_hnsw_index_options_applied(fresh: Any) -> None:
    import psycopg

    s = fresh(hnsw_m=24, hnsw_ef_construction=128)
    s.ensure_collection(CollectionSpec(name="rc_opts_0001", dim=8, metric="cosine"))
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = 'rc_opts_0001' "
            "AND indexdef LIKE '%%hnsw%%'",
            (s.schema,),
        ).fetchone()
    assert row is not None
    assert "m='24'" in row[0] and "ef_construction='128'" in row[0]


def test_factory_conninfo_from_parts(dsn: str, tmp_path: Any) -> None:
    t = vector_store_types()["pgvector"]
    cfg = t.config_model.model_validate(
        {"host": "127.0.0.1", "port": 54329, "dbname": "ragcentre", "user": "ragcentre", "sslmode": "disable"}
    )
    store = t.factory(cfg, {"password": "ragcentre"}, tmp_path)
    try:
        store.healthcheck()
    finally:
        store.close()


def test_config_fields_and_immutability() -> None:
    t = vector_store_types()["pgvector"]
    assert t.immutable_fields == ("vector_type", "hnsw_m", "hnsw_ef_construction")
    assert set(t.secret_keys) == {"dsn", "password"}
    m = t.config_model
    cfg = m.model_validate(
        {
            "vector_type": "halfvec",
            "hnsw_m": 32,
            "hnsw_ef_construction": 128,
            "ef_search": 60,
            "max_scan_tuples": 5000,
            "host": "db.example",
            "port": 5432,
            "dbname": "app",
            "user": "app",
            "sslmode": "require",
        }
    )
    assert cfg.vector_type == "halfvec"
    assert cfg.hnsw_m == 32 and cfg.hnsw_ef_construction == 128
    assert cfg.ef_search == 60 and cfg.max_scan_tuples == 5000
    assert cfg.sslmode == "require"
    for bad in (
        {"vector_type": "sparsevec"},
        {"hnsw_m": 1},
        {"hnsw_m": 101},
        {"hnsw_ef_construction": 3},
        {"ef_search": 0},
        {"ef_search": 1001},
        {"max_scan_tuples": 0},
        {"port": 70000},
        {"sslmode": "nope"},
        {"bogus": 1},
    ):
        with pytest.raises(ValidationError):
            m.model_validate(bad)


def test_max_dim_is_enforced(fresh: Any) -> None:
    s = fresh()
    assert s.capabilities.max_dim == VECTOR_MAX_DIM
    with pytest.raises(DimensionLimitExceeded):
        s.ensure_collection(CollectionSpec(name="rc_big_0001", dim=VECTOR_MAX_DIM + 1, metric="cosine"))
    assert s.collection_info("rc_big_0001") is None


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


def test_index_shaping_fields_are_immutable_through_the_api(client: Any) -> None:
    """vector_type / hnsw_* return 409 on update; connection and query-time fields stay mutable."""
    made = client.post(
        "/v1/providers",
        json={
            "name": "pg-immutable",
            "kind": "vector_store",
            "type": "pgvector",
            "config": {"vector_type": "auto", "hnsw_m": 16, "ef_search": 40, "host": "db1"},
        },
    )
    assert made.status_code == 201, made.text
    url = f"/v1/providers/{made.json()['id']}"
    for field, value in (("vector_type", "halfvec"), ("hnsw_m", 32), ("hnsw_ef_construction", 128)):
        refused = client.patch(url, json={"config": {field: value}})
        assert refused.status_code == 409, (field, refused.text)
        assert refused.json()["error"]["code"] == "immutable_field"
        assert refused.json()["error"]["details"] == {"field": field}
    accepted = client.patch(
        url,
        json={
            "config": {"vector_type": "auto", "hnsw_m": 16, "ef_search": 120, "max_scan_tuples": 9000, "host": "db2"}
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["config"]["ef_search"] == 120
    assert accepted.json()["config"]["host"] == "db2"
