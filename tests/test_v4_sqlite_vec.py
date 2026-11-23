"""sqlite_vec: collection-name prefix, old-layout compatibility, per-instance capabilities, dot."""

from __future__ import annotations

import json
import sqlite3
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from ragcentre.stores.base import CollectionSpec, Record, StoreError
from ragcentre.stores.registry import vector_store_types
from ragcentre.stores.sqlite_vec import SqliteVecStore, load_extension
from ragcentre.testing.vector_store_contract import VectorStoreContract

NAME = "rc_prefix_0001"


class TestSqliteVecPrefixedContract(VectorStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[SqliteVecStore]:
        s = SqliteVecStore(tmp_path / "vectors.db", prefix="tenant_a_")
        yield s
        s.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def test_prefix_forms_the_physical_table_name(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    s = SqliteVecStore(db, prefix="tenant_a_")
    s.ensure_collection(CollectionSpec(name=NAME, dim=2, metric="l2"))
    s.upsert(NAME, [Record("a", [1.0, 0.0], {})])
    assert "tenant_a_" + NAME in _tables(db)
    assert NAME not in _tables(db)
    conn = sqlite3.connect(db)
    assert [r[0] for r in conn.execute("SELECT name FROM _collections")] == ["tenant_a_" + NAME]
    conn.close()
    info = s.collection_info(NAME)
    assert info is not None and info.count == 1
    s.close()


def test_prefixes_isolate_collections_in_one_database(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    plain, a, b = SqliteVecStore(db), SqliteVecStore(db, prefix="a_"), SqliteVecStore(db, prefix="b_")
    for st, vec in ((plain, [1.0, 0.0]), (a, [0.0, 1.0]), (b, [1.0, 1.0])):
        st.ensure_collection(CollectionSpec(name=NAME, dim=2, metric="l2"))
        st.upsert(NAME, [Record(f"id{id(st)}", vec, {})])
    assert plain.collection_info(NAME) is not None
    a.drop_collection(NAME)
    assert a.collection_info(NAME) is None
    assert plain.collection_info(NAME) is not None and b.collection_info(NAME) is not None
    assert b.query(NAME, [1.0, 1.0], 5)[0].score == pytest.approx(0.0)
    for st in (plain, a, b):
        st.close()


@pytest.mark.parametrize(
    "prefix",
    ["sqlite_", "Upper_", "a-b_", "x" * 120, "_"],
)
def test_invalid_physical_names_raise_store_error(tmp_path: Path, prefix: str) -> None:
    s = SqliteVecStore(tmp_path / "v.db", prefix=prefix)
    # "_" plus "collections" is the catalog table itself: the only valid-looking collision.
    name = "collections" if prefix == "_" else NAME
    with pytest.raises(StoreError):
        s.ensure_collection(CollectionSpec(name=name, dim=2, metric="l2"))
    for call in (
        lambda: s.collection_info(name),
        lambda: s.drop_collection(name),
        lambda: s.delete(name, ["x"]),
        lambda: s.upsert(name, []),
        lambda: s.query(name, [0.0, 0.0], 1),
    ):
        with pytest.raises(StoreError):
            call()
    s.close()


def test_config_validates_prefix() -> None:
    t = vector_store_types()["sqlite_vec"]
    assert t.config_model.model_validate({"prefix": "rc_"}).prefix == "rc_"  # type: ignore[attr-defined]
    assert t.config_model.model_validate({}).prefix == ""  # type: ignore[attr-defined]
    for bad in ("Rc_", "a b", 'x"', "a-", "x" * 65):
        with pytest.raises(ValidationError):
            t.config_model.model_validate({"prefix": bad})


def test_factory_passes_prefix(tmp_path: Path) -> None:
    t = vector_store_types()["sqlite_vec"]
    store = t.factory(t.config_model.model_validate({"prefix": "rc_"}), {}, tmp_path)
    assert isinstance(store, SqliteVecStore)
    store.ensure_collection(CollectionSpec(name=NAME, dim=2, metric="cosine"))
    assert "rc_" + NAME in _tables(tmp_path / "stores" / "vectors.db")
    store.close()


def test_prefix_is_not_an_immutable_field() -> None:
    # Consistent with qdrant and chroma: prefix is a connection-level field, not an index option.
    assert vector_store_types()["sqlite_vec"].immutable_fields == ()


def _build_old_layout(path: Path) -> None:
    """The database as written by the code before ``prefix`` existed: logical name == table name."""
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    load_extension(conn)
    conn.execute(
        "CREATE TABLE _collections (name TEXT PRIMARY KEY, dim INTEGER NOT NULL, metric TEXT NOT NULL, "
        "fields TEXT NOT NULL)"
    )
    conn.execute(f'CREATE TABLE "{NAME}" (id TEXT PRIMARY KEY, vector BLOB NOT NULL, metadata TEXT NOT NULL)')
    conn.execute("INSERT INTO _collections VALUES (?, 2, 'cosine', ?)", (NAME, json.dumps({"lang": "str"})))
    for rid, vec, lang in (("a", [1.0, 0.0], "en"), ("b", [0.0, 1.0], "fr"), ("c", [0.9, 0.1], "fr")):
        conn.execute(
            f'INSERT INTO "{NAME}" VALUES (?, ?, ?)',
            (rid, struct.pack("2f", *vec), json.dumps({"lang": lang})),
        )
    conn.commit()
    conn.close()


def test_old_layout_database_still_opens_and_queries(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _build_old_layout(db)
    s = SqliteVecStore(db)  # empty prefix: physical name == logical name
    info = s.collection_info(NAME)
    assert info is not None and (info.dim, info.metric, info.count) == (2, "cosine", 3)
    got = s.query(NAME, [1.0, 0.0], 3)
    assert [m.id for m in got] == ["a", "c", "b"]
    assert got[0].score == pytest.approx(1.0)
    from ragcentre.models.filters import Cond

    fr = s.query(NAME, [1.0, 0.0], 3, Cond("lang", "$eq", "fr"))
    assert [m.id for m in fr] == ["c", "b"]
    # ensure_collection is idempotent against the old catalog row, and writes keep working.
    s.ensure_collection(CollectionSpec(name=NAME, dim=2, metric="cosine", fields={"lang": "str"}))
    s.upsert(NAME, [Record("d", [1.0, 0.0], {"lang": "en"})])
    assert s.collection_info(NAME).count == 4  # type: ignore[union-attr]
    s.close()
    assert _tables(db) >= {"_collections", NAME}


def test_prefixed_store_does_not_see_old_layout_collection(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _build_old_layout(db)
    s = SqliteVecStore(db, prefix="p_")
    assert s.collection_info(NAME) is None
    s.close()


def test_instance_capabilities(tmp_path: Path) -> None:
    s = SqliteVecStore(tmp_path / "v.db", prefix="p_")
    caps = s.capabilities
    assert caps.metrics == frozenset({"cosine", "l2"})
    assert caps.max_batch == 1000
    assert caps.max_dim is None  # sqlite-vec distance functions ran on 1,000,000 dimensions
    assert caps.consistency == "immediate" and caps.filter_exact is True and caps.max_collections is None
    assert vector_store_types()["sqlite_vec"].capabilities == SqliteVecStore.capabilities
    s.close()


def test_dot_is_unsupported_and_sqlite_vec_has_no_dot_function(tmp_path: Path) -> None:
    """The reason ``dot`` stays off: no dot-product distance, and ``vec_length`` is not a norm."""
    conn = sqlite3.connect(":memory:")
    load_extension(conn)
    functions = {r[0] for r in conn.execute("SELECT name FROM pragma_function_list WHERE name LIKE 'vec%'")}
    assert not {f for f in functions if "dot" in f or "inner" in f}
    vec = struct.pack("3f", 3.0, 4.0, 0.0)
    assert conn.execute("SELECT vec_length(?)", (vec,)).fetchone()[0] == 3  # element count, not 5.0
    conn.close()
    s = SqliteVecStore(tmp_path / "v.db")
    with pytest.raises(StoreError):
        s.ensure_collection(CollectionSpec(name="rc_dot_00001", dim=4, metric="dot"))
    s.close()
