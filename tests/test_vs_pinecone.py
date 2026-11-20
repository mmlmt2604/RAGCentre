"""Pinecone vector store tests.

Three layers:

* pure unit tests (config, filter compiler, scores, limits, catalog, index-description parsing);
* the full contract suite against a **fake client** (``FakePinecone``: an in-memory server that mimics
  what was observed on Pinecone Local, with a write delay to exercise eventual consistency and with
  the documented request limits enforced). It needs no server and no ``pinecone`` package;
* the full contract suite against a live **Pinecone Local** emulator, and (``cloud``) against a hosted
  Pinecone project. Both skip cleanly.

    docker run -d -e PORT=5080 -e PINECONE_HOST=localhost -p 5080-5100:5080-5100 \
        ghcr.io/pinecone-io/pinecone-local:latest
    RAGCENTRE_TEST_PINECONE_URL=http://localhost:5080 uv run pytest tests/test_vs_pinecone.py

The live tests create their own indexes (the adapter never does), named ``rct-<random>``, and delete
them afterwards. Hosted-Pinecone tests need ``RAGCENTRE_TEST_PINECONE_API_KEY`` (and optionally
``RAGCENTRE_TEST_PINECONE_CLOUD`` / ``..._REGION``, default aws / us-east-1): they create and delete
serverless indexes and bill the project, so they are opt-in. The key is read from the environment only.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import random
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import Cond, Filter, matches, parse_filter
from ragcentre.stores.base import (
    CollectionNotFound,
    CollectionSpec,
    Match,
    Metric,
    Record,
    StoreError,
    StoreUnavailable,
    VectorStore,
)
from ragcentre.stores.catalog import LocalCatalog
from ragcentre.stores.pinecone import (
    MAX_METADATA_BYTES,
    MAX_TOP_K,
    STORE_TYPE,
    PineconeConfig,
    PineconeStore,
    _disable_index_retries,
    check_metadata,
    check_record_id,
    compile_filter,
    shape_from_legacy_json,
    shape_from_model,
    to_score,
)
from ragcentre.testing.random_data import SCHEMA_FIELDS, full_schema, random_filter, random_metadata, random_vector
from ragcentre.testing.vector_store_contract import (
    DIM,
    N_RECORDS,
    SEED,
    VectorStoreContract,
    assert_same_results,
    brute_score,
    make_records,
    wait_until,
)

LOCAL_URL = os.environ.get("RAGCENTRE_TEST_PINECONE_URL", "http://localhost:5080")
CLOUD_KEY = os.environ.get("RAGCENTRE_TEST_PINECONE_API_KEY", "")
LOCAL_HEADERS = {"Api-Key": "pinecone-local", "X-Pinecone-Api-Version": "2025-10", "Content-Type": "application/json"}
PMETRIC: dict[str, str] = {"cosine": "cosine", "l2": "euclidean", "dot": "dotproduct"}


# ---- a stand-in for the `pinecone` module's errors, and an in-memory server ------------------------


class _PineconeError(Exception):
    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class _ApiError(_PineconeError):
    pass


class _NotFound(_ApiError):
    pass


class _Unauthorized(_ApiError):
    pass


class _Forbidden(_ApiError):
    pass


class _ResponseParsing(_PineconeError):
    pass


FAKE_PX = SimpleNamespace(
    PineconeError=_PineconeError,
    ApiError=_ApiError,
    NotFoundError=_NotFound,
    UnauthorizedError=_Unauthorized,
    ForbiddenError=_Forbidden,
    ResponseParsingError=_ResponseParsing,
)


def pinecone_matches(flt: dict[str, Any], md: dict[str, Any]) -> bool:
    """Pinecone's filter semantics as observed on Pinecone Local: ``$ne`` / ``$nin`` hold for a missing
    field, booleans compare only with booleans, lists of booleans are refused, ranges take numbers."""
    for key, cond in flt.items():
        if key == "$and":
            if not all(pinecone_matches(c, md) for c in cond):
                return False
            continue
        if key == "$or":
            if not any(pinecone_matches(c, md) for c in cond):
                return False
            continue
        if not isinstance(cond, dict):
            cond = {"$eq": cond}
        actual = md.get(key)
        for op, want in cond.items():
            if not _op_holds(op, actual, want):
                return False
    return True


def _same(a: Any, b: Any) -> bool:
    if a is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return bool(a == b)


def _op_holds(op: str, actual: Any, want: Any) -> bool:
    if op == "$eq":
        return _same(actual, want)
    if op == "$ne":
        return not _same(actual, want)
    if op in ("$in", "$nin"):
        if not isinstance(want, list) or any(isinstance(w, bool) for w in want):
            raise _ApiError(f"the {op} operator must be followed by a list of strings or a list of numbers")
        hit = any(_same(actual, w) for w in want)
        return hit if op == "$in" else not hit
    if op in ("$gt", "$gte", "$lt", "$lte"):
        if isinstance(want, bool) or not isinstance(want, (int, float)):
            raise _ApiError(f"the {op} operator must be followed by a number")
        if actual is None or isinstance(actual, (bool, str)):
            return False
        return {"$gt": actual > want, "$gte": actual >= want, "$lt": actual < want, "$lte": actual <= want}[op]
    raise _ApiError(f"unknown operator {op}")


class FakeServer:
    """One dense index: namespaces of vectors. Writes become visible ``delay`` seconds later."""

    def __init__(self, dim: int, metric: str, *, delay: float = 0.0, host: str = "fake-abc.svc.pinecone.io") -> None:
        self.dim, self.metric, self.delay, self.host = dim, metric, delay, host
        self.ns: dict[str, dict[str, tuple[list[float], dict[str, Any]]]] = {}
        self.pending: list[tuple[float, Any]] = []
        self.requests: list[tuple[str, int]] = []  # (kind, size)
        self.lock = threading.RLock()

    def apply_due(self) -> None:
        with self.lock:
            now = time.monotonic()
            due = [p for p in self.pending if p[0] <= now]
            self.pending = [p for p in self.pending if p[0] > now]
            for _, fn in due:
                fn()

    def later(self, fn: Any) -> None:
        with self.lock:
            self.pending.append((time.monotonic() + self.delay, fn))
        self.apply_due()


class FakeIndex:
    def __init__(self, server: FakeServer) -> None:
        self.s = server
        self.closed = False

    def upsert(self, *, vectors: Sequence[dict[str, Any]], namespace: str = "", **_: Any) -> Any:
        s = self.s
        size = len(json.dumps(list(vectors), separators=(",", ":")))
        s.requests.append(("upsert", size))
        if len(vectors) > 1000:
            raise _ApiError("too many vectors in one request")
        if size > 2 * 1024 * 1024:
            raise _ApiError("request is larger than 2 MB")
        for v in vectors:
            if len(v["values"]) != s.dim:
                raise _ApiError(
                    f"Vector dimension {len(v['values'])} does not match the dimension of the index {s.dim}"
                )
            if len(v["id"]) > 512 or not v["id"].isascii():
                raise _ApiError("invalid id")
            md = v.get("metadata") or {}
            if len(json.dumps(md).encode()) > 40960:
                raise _ApiError("metadata too large")
            if any(x is None or isinstance(x, (dict, list)) for x in md.values()):
                raise _ApiError("invalid metadata value")
            if s.metric == "cosine" and not any(v["values"]):
                pass  # like the emulator: accepted, but it cannot be scored later
        batch = [(v["id"], list(v["values"]), dict(v.get("metadata") or {})) for v in vectors]

        def write() -> None:
            space = s.ns.setdefault(namespace, {})
            for rid, vec, md in batch:
                space[rid] = (vec, md)

        s.later(write)
        return SimpleNamespace(upserted_count=len(vectors))

    def query(self, *, top_k: int, vector: Sequence[float], namespace: str = "", filter: dict[str, Any] | None = None,
              include_values: bool = False, include_metadata: bool = False, **_: Any) -> Any:  # fmt: skip
        s = self.s
        s.apply_due()
        if not 1 <= top_k <= MAX_TOP_K:
            raise _PineconeError("top_k out of range")
        if len(vector) != s.dim:
            raise _ApiError("dimension mismatch")
        scored: list[tuple[str, float]] = []
        with s.lock:
            space = dict(s.ns.get(namespace, {}))
        for rid, (vec, md) in space.items():
            if filter is not None and not pinecone_matches(filter, md):
                continue
            if s.metric == "euclidean":
                score = sum((a - b) ** 2 for a, b in zip(vec, vector, strict=True))
            elif s.metric == "dotproduct":
                score = sum(a * b for a, b in zip(vec, vector, strict=True))
            else:
                na, nb = math.sqrt(sum(a * a for a in vec)), math.sqrt(sum(b * b for b in vector))
                if na == 0 or nb == 0:
                    raise _ResponseParsing("Expected `float`, got `null` - at `$.matches[0].score`")
                score = sum(a * b for a, b in zip(vec, vector, strict=True)) / (na * nb)
            scored.append((rid, score))
        scored.sort(key=lambda x: x[1] if s.metric == "euclidean" else -x[1])  # best first
        return SimpleNamespace(matches=[SimpleNamespace(id=i, score=v) for i, v in scored[:top_k]])

    def delete(
        self, *, ids: Sequence[str] | None = None, delete_all: bool = False, namespace: str = "", **_: Any
    ) -> None:
        s = self.s
        s.requests.append(("delete", len(ids or [])))
        if len(ids or []) > 1000:
            raise _ApiError("too many ids")
        if namespace not in s.ns and not s.pending:
            raise _NotFound("Namespace not found")  # the hosted service answers 404 for an unknown namespace

        def run() -> None:
            if delete_all:
                s.ns.pop(namespace, None)
            else:
                for rid in ids or []:
                    s.ns.get(namespace, {}).pop(rid, None)

        s.later(run)

    def fetch(self, *, ids: Sequence[str], namespace: str = "", **_: Any) -> Any:
        s = self.s
        s.apply_due()
        with s.lock:
            space = dict(s.ns.get(namespace, {}))
        return SimpleNamespace(vectors={i: SimpleNamespace(metadata=space[i][1]) for i in ids if i in space})

    def describe_index_stats(self, **_: Any) -> Any:
        s = self.s
        s.apply_due()
        with s.lock:
            return SimpleNamespace(
                dimension=s.dim,
                namespaces={k: SimpleNamespace(vector_count=len(v)) for k, v in s.ns.items()},
            )

    def close(self) -> None:
        self.closed = True


class FakePinecone:
    """The few ``pinecone.Pinecone`` members the adapter uses. It has no way to create or delete an index."""

    def __init__(self, server: FakeServer | None, *, error: Exception | None = None) -> None:
        self.server = server
        self.error = error
        self.indexes = SimpleNamespace(describe=self._describe)
        self.opened: list[str] = []
        self.closed = False

    def _describe(self, name: str) -> Any:
        if self.error is not None:
            raise self.error
        if self.server is None:
            raise _NotFound(f"no index {name}")
        s = self.server
        return SimpleNamespace(
            name=name,
            host=s.host,
            status=SimpleNamespace(ready=True, state="Ready"),
            schema=SimpleNamespace(fields={"_values": SimpleNamespace(dimension=s.dim, metric=s.metric)}),
        )

    def index(self, *, host: str) -> FakeIndex:
        assert self.server is not None
        self.opened.append(host)
        return FakeIndex(self.server)

    def close(self) -> None:
        self.closed = True


def make_fake_store(
    tmp_path: Path, *, dim: int = DIM, metric: str = "cosine", delay: float = 0.0, prefix: str = "", name: str = "idx",
    server: FakeServer | None = None,
) -> tuple[PineconeStore, FakeServer]:  # fmt: skip
    server = server or FakeServer(dim, metric, delay=delay)
    pc = FakePinecone(server)
    catalog = LocalCatalog(tmp_path, "pinecone", instance_key=f"fake/{name}")
    store = PineconeStore(pc, FAKE_PX, catalog, index_name=name, prefix=prefix, api_key="k-secret")
    return store, server


# ---- unit tests: config ----------------------------------------------------------------------------


def test_config_defaults_and_validation() -> None:
    cfg = PineconeConfig(index_name="my-index")
    assert (cfg.prefix, cfg.controller_host, cfg.index_host, cfg.timeout) == ("", None, None, 30.0)
    with pytest.raises(ValidationError):
        PineconeConfig()  # type: ignore[call-arg]  # index_name is required
    for bad in ["", "My-Index", "-a", "a-", "a_b", "x" * 46, "a b"]:
        with pytest.raises(ValidationError):
            PineconeConfig(index_name=bad)
    for bad in ["a b", "ü", "x" * 65, "a/b"]:
        with pytest.raises(ValidationError):
            PineconeConfig(index_name="ok", prefix=bad)
    PineconeConfig(index_name="ok", prefix="team.a:b_c-1")
    PineconeConfig(index_name="ok", controller_host="http://localhost:5080", index_host="x.svc.pinecone.io")
    assert PineconeConfig(index_name="ok", controller_host="").controller_host is None
    for bad in ["http://user:pw@host", "http://h?x=1", "http://", "ht tp://x", "http://http://x"]:
        with pytest.raises(ValidationError):
            PineconeConfig(index_name="ok", controller_host=bad)
    with pytest.raises(ValidationError):
        PineconeConfig(index_name="ok", timeout=0)
    with pytest.raises(ValidationError):
        PineconeConfig(index_name="ok", surprise=1)  # type: ignore[call-arg]


def test_store_type_registration() -> None:
    from ragcentre.stores.registry import BUILTIN  # noqa: F401  (the lead adds the BUILTIN line)

    assert STORE_TYPE.type == "pinecone"
    assert STORE_TYPE.secret_keys == ("api_key",)
    assert STORE_TYPE.immutable_fields == ()
    caps = STORE_TYPE.capabilities
    assert caps.metrics == frozenset({"cosine", "l2", "dot"})
    assert (caps.max_batch, caps.max_dim, caps.consistency, caps.filter_exact) == (1000, 20000, "eventual", False)
    assert "index_name" in STORE_TYPE.config_model.model_json_schema()["required"]


def test_import_name_is_the_client_not_this_module() -> None:
    pytest.importorskip("pinecone")
    import importlib

    import ragcentre.stores.pinecone as ours

    client = importlib.import_module("pinecone")
    assert client is not ours
    assert Path(client.__file__ or "").parent.name == "pinecone" and "ragcentre" not in str(client.__file__)
    assert hasattr(client, "Pinecone") and hasattr(client, "NotFoundError")


def test_missing_extra_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "pinecone", None)  # makes `import pinecone` raise ImportError
    with pytest.raises(StoreUnavailable, match=r"ragcentre\[pinecone\]"):
        PineconeStore.connect(index_name="x", data_dir=Path("."), api_key="k")


# ---- unit tests: filter compiler, scores, limits ------------------------------------------------


def _compile(raw: Any) -> dict[str, Any]:
    f = parse_filter(raw, full_schema())
    assert f is not None
    return compile_filter(f)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"lang": "en"}, {"lang": {"$eq": "en"}}),
        ({"lang": {"$ne": "en"}}, {"lang": {"$ne": "en"}}),
        ({"year": {"$gte": 2020}}, {"year": {"$gte": 2020}}),
        ({"score": {"$lt": 0.5}}, {"score": {"$lt": 0.5}}),
        ({"lang": {"$in": ["en", "fr"]}}, {"lang": {"$in": ["en", "fr"]}}),
        ({"year": {"$nin": [2020, 2021]}}, {"year": {"$nin": [2020, 2021]}}),
        ({"public": True}, {"public": {"$eq": True}}),
        ({"public": {"$ne": False}}, {"public": {"$ne": False}}),
        # Pinecone refuses lists of booleans in $in / $nin (observed): expanded.
        ({"public": {"$in": [True]}}, {"$or": [{"public": {"$eq": True}}]}),
        ({"public": {"$in": [True, False]}}, {"$or": [{"public": {"$eq": True}}, {"public": {"$eq": False}}]}),
        ({"public": {"$nin": [True, False]}}, {"$and": [{"public": {"$ne": True}}, {"public": {"$ne": False}}]}),
        (
            {"$and": [{"lang": "en"}, {"$or": [{"year": {"$gt": 2020}}, {"public": True}]}]},
            {"$and": [{"lang": {"$eq": "en"}}, {"$or": [{"year": {"$gt": 2020}}, {"public": {"$eq": True}}]}]},
        ),
    ],
)
def test_filter_compiler_shapes(raw: Any, expected: dict[str, Any]) -> None:
    assert _compile(raw) == expected


def test_filter_compiler_rejects_unsafe_numbers() -> None:
    with pytest.raises(StoreError, match="2\\*\\*53"):
        compile_filter(Cond("year", "$eq", 2**60))
    with pytest.raises(StoreError, match="finite"):
        compile_filter(Cond("score", "$gt", float("inf")))


def test_compiled_filters_agree_with_the_reference_evaluator() -> None:
    """Random filters over random metadata (some fields dropped): Pinecone's semantics, as implemented by the
    fake and observed on the emulator, give the same answer as ``models.filters.matches``."""
    rng = random.Random(SEED)
    schema = full_schema()
    checked = 0
    for _ in range(1500):
        f = parse_filter(random_filter(rng), schema)
        assert f is not None
        compiled = compile_filter(f)
        for _ in range(4):
            md = random_metadata(rng)
            for key in list(md):
                if key != "document_id" and rng.random() < 0.2:
                    del md[key]  # records written before a field existed
            assert pinecone_matches(compiled, md) == matches(f, md), (f, md)
            checked += 1
    assert checked == 6000


def test_to_score_matches_the_verified_scales() -> None:
    assert to_score("cosine", 0.8017837) == 0.8017837
    assert to_score("dot", 3.0) == 3.0
    assert to_score("l2", 9.0) == -3.0  # euclidean comes back squared (observed: |[1,0,0]-[3,2,1]|^2 = 9)
    assert to_score("l2", 0.0) == 0.0
    assert to_score("l2", -1e-12) == 0.0  # rounding can make a squared distance slightly negative


def test_record_id_limits() -> None:
    assert check_record_id("a" * 512) == "a" * 512
    for bad in ["", "a" * 513, "é", "a\x00b"]:
        with pytest.raises(StoreError, match="ASCII"):
            check_record_id(bad)


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"a": None}, "unsupported type NoneType"),
        ({"a": [1]}, "unsupported type list"),
        ({"a": {"b": 1}}, "unsupported type dict"),
        ({"a": 2**53 + 1}, "2\\*\\*53"),
        ({"a": -(2**60)}, "2\\*\\*53"),
        ({"a": float("nan")}, "finite"),
        ({"a": float("inf")}, "finite"),
        ({"$a": 1}, "not starting with"),
        ({"": 1}, "non-empty"),
        ({"a": "x" * (MAX_METADATA_BYTES + 1)}, "bytes"),
        ({"a": "é" * (MAX_METADATA_BYTES // 2)}, "bytes"),  # counted in UTF-8 bytes, not characters
    ],
)
def test_metadata_limits(metadata: dict[str, Any], message: str) -> None:
    with pytest.raises(StoreError, match=message):
        check_metadata("rid", metadata)


def test_metadata_accepts_what_the_core_sends() -> None:
    check_metadata("rid", {"document_id": "d", "lang": "", "year": 2**53, "score": 0.5, "public": False, "n": -3})


# ---- unit tests: index description -----------------------------------------------------------------


def _model(fields: dict[str, Any], *, ready: bool = True, host: str | None = "idx-1.svc.pinecone.io") -> Any:
    return SimpleNamespace(
        status=SimpleNamespace(ready=ready, state="Ready" if ready else "Initializing"),
        schema=SimpleNamespace(fields=fields),
        host=host,
    )


def test_shape_from_model() -> None:
    dense = SimpleNamespace(dimension=1536, metric="dotproduct")
    shape = shape_from_model("idx", _model({"_values": dense}), None)
    assert (shape.dim, shape.metric, shape.host) == (1536, "dot", "https://idx-1.svc.pinecone.io")
    assert shape_from_model("idx", _model({"_values": dense}), "http://10.0.0.5:8080").host == "http://10.0.0.5:8080"
    assert shape_from_model("idx", _model({"_values": SimpleNamespace(dimension=3, metric="euclidean"),
                                           "_sparse_values": object()}), None).metric == "l2"  # fmt: skip
    with pytest.raises(StoreError, match="not ready"):
        shape_from_model("idx", _model({"_values": dense}, ready=False), None)
    with pytest.raises(StoreError, match="no host"):
        shape_from_model("idx", _model({"_values": dense}, host=None), None)
    for fields in ({}, {"embedding": dense}, {"_values": dense, "title": object()}, {"_sparse_values": object()}):
        with pytest.raises(StoreError, match="plain dense vector index"):
            shape_from_model("idx", _model(fields), None)


def test_shape_from_real_client_model() -> None:
    """The 2026-07 describe response, decoded by the real client's own model class."""
    pytest.importorskip("pinecone")
    import msgspec
    from pinecone.models.indexes.index import IndexModel

    payload = {
        "name": "idx",
        "host": "idx-abc.svc.aped-1234.pinecone.io",
        "status": {"ready": True, "state": "Ready"},
        "deletion_protection": "disabled",
        "schema": {"fields": {"_values": {"type": "dense_vector", "dimension": 8, "metric": "euclidean"}}},
        "deployment": {"deployment_type": "managed", "cloud": "aws", "region": "us-east-1"},
    }
    model = msgspec.json.decode(json.dumps(payload).encode(), type=IndexModel)
    shape = shape_from_model("idx", model, None)
    assert (shape.dim, shape.metric, shape.host) == (8, "l2", "https://idx-abc.svc.aped-1234.pinecone.io")
    payload["schema"] = {"fields": {"vec": {"type": "dense_vector", "dimension": 8, "metric": "cosine"}}}
    other = msgspec.json.decode(json.dumps(payload).encode(), type=IndexModel)
    with pytest.raises(StoreError, match="plain dense vector index"):  # a document index: other API
        shape_from_model("idx", other, None)


def test_shape_from_legacy_json() -> None:
    body = {"name": "i", "dimension": 8, "metric": "cosine", "host": "localhost:5081", "status": {"ready": True}}
    shape = shape_from_legacy_json("i", body, "http://localhost:5080", None)
    assert (shape.dim, shape.metric, shape.host) == (8, "cosine", "http://localhost:5081")
    assert shape_from_legacy_json("i", body, "https://c", None).host == "https://localhost:5081"
    assert shape_from_legacy_json("i", body, "http://c", "http://x:1/").host == "http://x:1"
    for bad in ({**body, "metric": "manhattan"}, {**body, "dimension": "8"}, {**body, "vector_type": "sparse"},
                {**body, "status": {"ready": False, "state": "Init"}}, {**body, "host": ""}, []):  # fmt: skip
        with pytest.raises(StoreError):
            shape_from_legacy_json("i", bad, "http://c", None)


# ---- unit tests: the adapter on the fake client -----------------------------------------------------


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[tuple[PineconeStore, FakeServer]]:
    store, server = make_fake_store(tmp_path, prefix="p_")
    yield store, server
    store.close()


def _spec(name: str = "rc_unit_0001", dim: int = DIM, metric: Metric = "cosine", **kw: Any) -> CollectionSpec:
    return CollectionSpec(name, dim, metric, kw.get("fields", dict(SCHEMA_FIELDS)))


def _rec(rid: str = "r1", vec: Sequence[float] | None = None, **md: Any) -> Record:
    meta = {"document_id": "d", "lang": "en", "year": 2020, "score": 0.5, "public": True, **md}
    return Record(rid, list(vec) if vec is not None else [1.0] + [0.0] * (DIM - 1), meta)


def test_instance_capabilities_follow_the_index(tmp_path: Path) -> None:
    for metric, want in (("cosine", "cosine"), ("euclidean", "l2"), ("dotproduct", "dot")):
        store, _ = make_fake_store(tmp_path, metric=metric, name=f"cap-{metric}")
        caps = store.capabilities
        assert caps.metrics == frozenset({want})  # a Pinecone index has exactly one metric
        assert (caps.max_batch, caps.max_dim, caps.consistency, caps.filter_exact) == (1000, 20000, "eventual", False)
        assert STORE_TYPE.capabilities.metrics == frozenset({"cosine", "l2", "dot"})  # the type's best case
        store.close()
    # An unreachable or missing index: the best case, and the next read tries again.
    down = PineconeStore(FakePinecone(None), FAKE_PX, LocalCatalog(tmp_path, "pinecone", "x/down"), index_name="down")
    assert down.capabilities == PineconeStore.BEST_CASE
    down._pc.server = FakeServer(DIM, "euclidean")
    assert down.capabilities.metrics == frozenset({"l2"})
    down.close()


def test_namespace_is_prefix_plus_name_and_catalog_holds_fields(fake: tuple[PineconeStore, FakeServer]) -> None:
    store, server = fake
    store.ensure_collection(_spec())
    assert server.ns == {}  # ensure_collection writes nothing to the index
    assert server.requests == []
    store.upsert("rc_unit_0001", [_rec()])
    assert list(server.ns) == ["p_rc_unit_0001"]
    entry = store._catalog.get("rc_unit_0001")
    assert entry is not None and entry.fields == SCHEMA_FIELDS and entry.options == {}
    info = store.collection_info("rc_unit_0001")
    assert info is not None and (info.dim, info.metric, info.count) == (DIM, "cosine", 1)


def test_default_namespace_alias_is_refused(tmp_path: Path) -> None:
    store, _ = make_fake_store(tmp_path, prefix="__")
    with pytest.raises(StoreError, match="namespace"):
        store.ensure_collection(_spec("default__"))
    store.close()


@pytest.mark.parametrize(("dim", "metric"), [(DIM + 1, "cosine"), (DIM, "l2"), (DIM, "dot")])
def test_ensure_collection_mismatch_names_the_index(tmp_path: Path, dim: int, metric: Metric) -> None:
    store, server = make_fake_store(tmp_path, name="docs-prod")
    with pytest.raises(CollectionMismatch, match="docs-prod"):
        store.ensure_collection(_spec(dim=dim, metric=metric))
    assert store.collection_info("rc_unit_0001") is None  # nothing was recorded
    assert server.requests == [] and server.ns == {}
    store.close()


def test_missing_index_says_that_ragcentre_never_creates_it(tmp_path: Path) -> None:
    pc = FakePinecone(None)
    catalog = LocalCatalog(tmp_path, "pinecone", instance_key="x/nope")
    store = PineconeStore(pc, FAKE_PX, catalog, index_name="nope")
    with pytest.raises(StoreError, match="never creates Pinecone indexes"):
        store.ensure_collection(_spec())
    with pytest.raises(StoreError, match="never creates"):
        store.healthcheck()
    assert not hasattr(pc.indexes, "create") and not hasattr(pc.indexes, "delete")
    store.close()


def test_unsupported_index_shapes_are_refused(tmp_path: Path) -> None:
    class Odd(FakePinecone):
        def _describe(self, name: str) -> Any:
            return _model({"title": object()})

    store = PineconeStore(
        Odd(FakeServer(DIM, "cosine")), FAKE_PX, LocalCatalog(tmp_path, "pinecone", "x/y"), index_name="y"
    )
    with pytest.raises(StoreError, match="plain dense vector index"):
        store.ensure_collection(_spec())
    store.close()


def test_catalog_round_trip_across_store_instances(tmp_path: Path) -> None:
    server = FakeServer(DIM, "cosine")
    a, _ = make_fake_store(tmp_path, server=server, name="one")
    a.ensure_collection(_spec(fields={"lang": "str"}))
    a.upsert("rc_unit_0001", [_rec()])
    a.close()
    b, _ = make_fake_store(tmp_path, server=server, name="one")  # a new process: same data dir
    b.ensure_collection(_spec(fields={"lang": "str", "year": "int"}))  # additive
    entry = b._catalog.get("rc_unit_0001")
    assert entry is not None and entry.fields == {"lang": "str", "year": "int"}
    with pytest.raises(CollectionMismatch, match="lang"):
        b.ensure_collection(_spec(fields={"lang": "int"}))
    other, _ = make_fake_store(tmp_path, server=server, name="two")  # same file, another index: separate keys
    assert other.collection_info("rc_unit_0001") is None
    b.drop_collection("rc_unit_0001")
    assert b._catalog.get("rc_unit_0001") is None and b.collection_info("rc_unit_0001") is None
    for s in (b, other):
        s.close()


@pytest.fixture
def connect_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Exercise connect's catalog ownership with the existing in-memory data plane.

    The SDK transport replacement is tested separately against the real SDK below.
    """
    import ragcentre.stores.pinecone as adapter

    stores: list[PineconeStore] = []
    servers: dict[str, FakeServer] = {}

    def client(**kwargs: Any) -> FakePinecone:
        assert kwargs["retry_config"].max_retries == 0
        return FakePinecone(servers[kwargs["api_key"]])

    px = SimpleNamespace(**vars(FAKE_PX), Pinecone=client, RetryConfig=SimpleNamespace)
    monkeypatch.setattr(adapter, "_import", lambda: px)
    monkeypatch.setattr(adapter, "_disable_index_retries", lambda index, px: None)

    def connect(server: FakeServer, *, key: str = "original-key", **kwargs: Any) -> PineconeStore:
        servers[key] = server
        store = PineconeStore.connect(index_name="shared-index", data_dir=tmp_path, api_key=key, **kwargs)
        stores.append(store)
        assert store._shape is None and store._pc.opened == []  # no resolution during connect
        return store

    yield connect
    for store in stores:
        store.close()


def test_connect_catalog_prefixes_isolate_entries_and_drop(connect_fake: Any) -> None:
    server = FakeServer(DIM, "cosine")
    a = connect_fake(server, prefix="a_")
    b = connect_fake(server, prefix="b_")
    name = "rc_unit_0001"
    a.ensure_collection(_spec(fields={"lang": "str"}))
    a.upsert(name, [_rec()])
    assert b.collection_info(name) is None
    with pytest.raises(CollectionNotFound):
        b.query(name, _rec().vector, 1)
    b.delete(name, ["r1"])
    assert a.collection_info(name).count == 1
    b.ensure_collection(_spec(fields={"lang": "int"}))  # incompatible fields are independent
    b.upsert(name, [_rec(lang=7)])
    assert a._catalog.get(name).fields == {"lang": "str"}
    b.drop_collection(name)
    assert b.collection_info(name) is None
    assert a.collection_info(name).count == 1
    assert a.query(name, _rec().vector, 1)[0].id == "r1"


def test_connect_catalog_hosts_isolate_projects_with_the_same_index_name(connect_fake: Any) -> None:
    a = connect_fake(FakeServer(DIM, "cosine", host="project-a.svc.pinecone.io"), key="project-a")
    b = connect_fake(FakeServer(DIM, "cosine", host="project-b.svc.pinecone.io"), key="project-b")
    name = "rc_unit_0001"
    a.ensure_collection(_spec(fields={"lang": "str"}))
    a.upsert(name, [_rec()])
    assert b.collection_info(name) is None
    b.ensure_collection(_spec(fields={"lang": "int"}))
    b.upsert(name, [_rec(lang=7)])
    b.drop_collection(name)
    assert b.collection_info(name) is None
    assert a.collection_info(name).count == 1
    assert a._catalog.get(name).fields == {"lang": "str"}
    assert a._catalog.instance_key != b._catalog.instance_key


@pytest.mark.parametrize("operation", ["collection_info", "query", "upsert", "delete"])
def test_connect_catalog_resolves_before_first_read_and_survives_key_rotation(
    connect_fake: Any,
    operation: str,
) -> None:
    server = FakeServer(DIM, "cosine")
    original = connect_fake(server, prefix="p_")
    name = "rc_unit_0001"
    original.ensure_collection(_spec())
    original.upsert(name, [_rec()])
    original_key = original._catalog.instance_key
    # A fresh store must discover the existing entry before its first catalog read.
    rotated = connect_fake(server, key="rotated-key", prefix="p_")
    if operation == "collection_info":
        assert rotated.collection_info(name).count == 1
    elif operation == "query":
        assert rotated.query(name, _rec().vector, 1)[0].id == "r1"
    elif operation == "upsert":
        rotated.upsert(name, [_rec("r2")])
        assert rotated.collection_info(name).count == 2
    else:
        rotated.delete(name, ["r1"])
        assert rotated.collection_info(name).count == 0
    assert rotated._catalog.instance_key == original_key
    assert "original-key" not in original_key and "rotated-key" not in original_key
    assert rotated._catalog.get(name).fields == SCHEMA_FIELDS


def test_connect_legacy_catalog_requires_explicit_safe_reattachment(connect_fake: Any, tmp_path: Path) -> None:
    store = connect_fake(FakeServer(DIM, "cosine"), prefix="team_")
    legacy = LocalCatalog(tmp_path, "pinecone", "pinecone/shared-index")
    name = "rc_unit_0001"
    try:
        legacy.ensure(_spec())
        for operation in (
            lambda: store.collection_info(name),
            lambda: store.query(name, _rec().vector, 1),
            lambda: store.delete(name, ["r1"]),
        ):
            with pytest.raises(StoreError, match=r"reindex.*ensure_collection"):
                operation()
        # The schema supplied by the owning pipeline safely reattaches the namespace.
        store.ensure_collection(_spec())
        assert store.collection_info(name) is not None
        assert store._catalog.get(name).fields == SCHEMA_FIELDS
        # Keep the ambiguous old entry: another prefix/project may still own it.
        assert legacy.get(name) is not None
    finally:
        legacy.close()


def test_connect_catalog_rebinds_when_a_refreshed_host_changes(connect_fake: Any) -> None:
    store = connect_fake(FakeServer(DIM, "cosine", host="original.svc.pinecone.io"))
    store.ensure_collection(_spec())
    key = store._catalog.instance_key
    store._pc.server = FakeServer(DIM, "cosine", host="replacement.svc.pinecone.io")
    store.healthcheck()
    assert store._catalog.instance_key != key
    assert store.collection_info("rc_unit_0001") is None


def test_lost_catalog_orphans_the_collection_but_is_rebuilt_by_ensure(tmp_path: Path) -> None:
    server = FakeServer(DIM, "cosine")
    a, _ = make_fake_store(tmp_path, server=server)
    a.ensure_collection(_spec())
    a.upsert("rc_unit_0001", [_rec()])
    a.close()
    (tmp_path / "store_catalogs" / "pinecone.db").unlink()
    b, _ = make_fake_store(tmp_path, server=server)
    assert b.collection_info("rc_unit_0001") is None
    with pytest.raises(CollectionNotFound):
        b.upsert("rc_unit_0001", [_rec()])
    b.ensure_collection(_spec())
    assert b.collection_info("rc_unit_0001") is not None
    b.close()


def test_drop_empties_the_namespace_and_is_idempotent(fake: tuple[PineconeStore, FakeServer]) -> None:
    store, server = fake
    store.drop_collection("rc_never_0001")  # unknown namespace: the hosted service answers 404, which is fine
    store.ensure_collection(_spec())
    store.upsert("rc_unit_0001", [_rec()])
    store.drop_collection("rc_unit_0001")
    assert "p_rc_unit_0001" not in server.ns
    store.drop_collection("rc_unit_0001")
    assert store.collection_info("rc_unit_0001") is None


def test_delete_ignores_unknown_namespace_and_ids(fake: tuple[PineconeStore, FakeServer]) -> None:
    store, server = fake
    store.delete("rc_unit_0001", ["x"])  # not a collection
    store.ensure_collection(_spec())
    store.delete("rc_unit_0001", ["nothing-here"])  # the namespace does not exist yet: 404 is swallowed
    store.upsert("rc_unit_0001", [_rec("a"), _rec("b")])
    store.delete("rc_unit_0001", ["a", "missing"])
    assert list(server.ns["p_rc_unit_0001"]) == ["b"]
    store.delete("rc_unit_0001", [])


def test_upsert_is_split_by_count_and_size(tmp_path: Path) -> None:
    dim = 4000  # about 90 KB per vector once encoded: the 2 MB request limit binds long before 1,000 vectors
    store, server = make_fake_store(tmp_path, dim=dim)
    store.ensure_collection(_spec(dim=dim))
    rng = random.Random(1)
    records = [Record(f"id{i}", [rng.random() + 0.1 for _ in range(dim)], {"document_id": "d"}) for i in range(100)]
    store.upsert("rc_unit_0001", records)  # the fake raises on any request over 2 MB or 1,000 vectors
    sizes = [n for kind, n in server.requests if kind == "upsert"]
    assert len(sizes) > 3 and max(sizes) <= 2 * 1024 * 1024
    assert len(server.ns["rc_unit_0001"]) == 100
    store.close()

    small, srv = make_fake_store(tmp_path, name="small")
    small.ensure_collection(_spec())
    small.upsert("rc_unit_0001", [_rec(f"i{i}") for i in range(1000)])
    assert [kind for kind, _ in srv.requests] == ["upsert"]  # exactly one request of 1,000
    with pytest.raises(StoreError, match="max_batch"):
        small.upsert("rc_unit_0001", [_rec(f"j{i}") for i in range(1001)])
    assert len(srv.ns["rc_unit_0001"]) == 1000
    small.close()


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (_rec("é"), "ASCII"),
        (_rec("a" * 513), "ASCII"),
        (_rec(vec=[1.0] * (DIM - 1)), "dimension"),
        (_rec(vec=[0.0] * DIM), "all-zero"),
        (_rec(vec=[float("nan")] + [1.0] * (DIM - 1)), "finite"),
        (_rec(big="x" * 41000), "bytes"),
        (_rec(big=2**60), "2\\*\\*53"),
    ],
)
def test_upsert_validates_before_sending_anything(
    fake: tuple[PineconeStore, FakeServer], record: Record, message: str
) -> None:
    store, server = fake
    store.ensure_collection(_spec())
    with pytest.raises(StoreError, match=message):
        store.upsert("rc_unit_0001", [_rec("fine"), record])
    assert server.requests == []  # the good record was not sent either: the batch is refused whole


def test_query_validation(fake: tuple[PineconeStore, FakeServer]) -> None:
    store, server = fake
    store.ensure_collection(_spec())
    store.upsert("rc_unit_0001", [_rec()])
    q = [1.0] + [0.0] * (DIM - 1)
    assert store.query("rc_unit_0001", q, 0) == [] and store.query("rc_unit_0001", q, -3) == []
    with pytest.raises(StoreError, match="10000"):
        store.query("rc_unit_0001", q, MAX_TOP_K + 1)
    with pytest.raises(StoreError, match="dimension"):
        store.query("rc_unit_0001", q[:-1], 3)
    with pytest.raises(StoreError, match="all-zero"):
        store.query("rc_unit_0001", [0.0] * DIM, 3)
    with pytest.raises(CollectionNotFound):
        store.query("rc_missing_0001", q, 3)
    assert len(store.query("rc_unit_0001", q, MAX_TOP_K)) == 1
    assert [kind for kind, _ in server.requests] == ["upsert"]


def test_filters_that_pinecone_refuses_surface_as_store_errors(fake: tuple[PineconeStore, FakeServer]) -> None:
    store, _ = fake
    store.ensure_collection(_spec())
    store.upsert("rc_unit_0001", [_rec()])
    q = [1.0] + [0.0] * (DIM - 1)
    ok = parse_filter({"public": {"$in": [True, False]}}, full_schema())
    assert [m.id for m in store.query("rc_unit_0001", q, 5, ok)] == ["r1"]  # expanded to $or of $eq


def test_l2_scores_are_negative_euclidean_distance(tmp_path: Path) -> None:
    store, _ = make_fake_store(tmp_path, metric="euclidean")
    store.ensure_collection(_spec(metric="l2"))
    far, near = _rec("far", [3.0, 2.0, 1.0] + [0.0] * (DIM - 3)), _rec("near", [1.0, 1.0] + [0.0] * (DIM - 2))
    store.upsert("rc_unit_0001", [far, near])
    got = store.query("rc_unit_0001", [1.0] + [0.0] * (DIM - 1), 5)
    assert [m.id for m in got] == ["near", "far"]
    assert got[0].score == pytest.approx(-1.0) and got[1].score == pytest.approx(-math.sqrt(9.0))
    store.close()


def test_errors_never_carry_the_api_key_and_auth_gets_a_hint(tmp_path: Path) -> None:
    pc = FakePinecone(None, error=_Unauthorized("Invalid API key k-secret"))
    store = PineconeStore(pc, FAKE_PX, LocalCatalog(tmp_path, "pinecone", "x/y"), index_name="y", api_key="k-secret")
    with pytest.raises(StoreError) as info:
        store.healthcheck()
    assert "k-secret" not in str(info.value) and "***" in str(info.value) and "api_key secret" in str(info.value)
    forbidden = PineconeStore(
        FakePinecone(None, error=_Forbidden("no")), FAKE_PX, LocalCatalog(tmp_path, "pinecone", "x/z"), index_name="z"
    )
    with pytest.raises(StoreError, match="api_key secret"):
        forbidden.healthcheck()
    store.close()
    forbidden.close()


def test_eventual_consistency_is_real_on_the_fake_and_settle_waits(tmp_path: Path) -> None:
    store, _ = make_fake_store(tmp_path, delay=0.15)
    store.ensure_collection(_spec())
    store.upsert("rc_unit_0001", [_rec("a"), _rec("b")])
    q = [1.0] + [0.0] * (DIM - 1)
    assert store.query("rc_unit_0001", q, 5) == []  # not visible yet
    info = store.collection_info("rc_unit_0001")
    assert info is not None and info.count == 0
    wait_until(lambda: (store.collection_info("rc_unit_0001") or SimpleNamespace(count=0)).count == 2, timeout=5)
    assert {m.id for m in store.query("rc_unit_0001", q, 5)} == {"a", "b"}
    store.close()


def test_connect_builds_a_store_without_a_server(tmp_path: Path) -> None:
    pytest.importorskip("pinecone")
    cfg = PineconeConfig(index_name="docs", prefix="a_", controller_host="http://127.0.0.1:1")
    store = STORE_TYPE.factory(cfg, {}, tmp_path)  # no api_key needed for a local controller
    assert isinstance(store, PineconeStore)
    assert (tmp_path / "store_catalogs" / "pinecone.db").exists()
    with pytest.raises(StoreError, match=r"127\.0\.0\.1|connect|Connection|failed"):
        store.healthcheck()  # nothing listens there; the error must be a StoreError, not a raw client error
    store.close()
    with pytest.raises(StoreError, match="api_key secret is required"):
        STORE_TYPE.factory(PineconeConfig(index_name="docs"), {}, tmp_path)


@pytest.mark.parametrize("operation", ["describe", "query", "stats"])
@pytest.mark.parametrize("failure", [429, 503, "connection"])
def test_real_sdk_reads_never_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure: int | str,
) -> None:
    pytest.importorskip("pinecone")
    from pinecone._internal import http_client

    calls: list[httpx.Request] = []

    def request(transport: Any, req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if failure == "connection":
            raise httpx.ConnectError("simulated connection failure", request=req)
        return httpx.Response(failure, json={"message": "unavailable"}, request=req)

    def no_sleep(delay: float) -> None:
        pytest.fail("SDK attempted retry backoff")

    # Patch below the SDK's retry transport, so a hidden retry increments calls too.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", request)
    monkeypatch.setattr(http_client, "_retry_sleep", no_sleep)
    store = PineconeStore.connect(index_name="idx", data_dir=tmp_path, api_key="test-key")
    try:
        assert calls == []
        if operation != "describe":
            import ragcentre.stores.pinecone as adapter

            monkeypatch.setattr(
                store,
                "_describe",
                lambda: adapter.IndexShape(DIM, "cosine", "https://idx.svc.pinecone.io"),
            )
            store.ensure_collection(_spec())
            assert calls == []  # index construction and transport replacement are also lazy
            assert store._index._config.retry_config.max_retries == 0
        with pytest.raises(StoreError, match="failed"):
            if operation == "describe":
                store.healthcheck()
            elif operation == "query":
                store.query("rc_unit_0001", _rec().vector, 1)
            else:
                store.collection_info("rc_unit_0001")
        assert len(calls) == 1
        assert (
            calls[0].url.path
            == {"describe": "/indexes/idx", "query": "/query", "stats": "/describe_index_stats"}[operation]
        )
        assert store._pc._config.retry_config.max_retries == 0
    finally:
        store.close()


def test_sdk_retry_seam_preserves_transport_settings_and_closes_original() -> None:
    px = pytest.importorskip("pinecone")
    index = px.Index(
        api_key="test-key",
        host="https://idx.svc.pinecone.io",
        timeout=7,
        additional_headers={"X-Custom": "value"},
        source_tag="ragcentre",
        connection_pool_maxsize=13,
    )
    original = index._http
    config = index._config
    try:
        _disable_index_retries(index, px)
        assert original._client.is_closed
        assert index._http is not original
        assert index._http._headers == original._headers  # includes original API version and custom headers
        from dataclasses import replace

        assert index._config == replace(config, retry_config=px.RetryConfig(max_retries=0))
        assert index._http._config == index._config
    finally:
        index.close()


@pytest.mark.parametrize("broken", ["http", "config", "api_version"])
def test_sdk_retry_seam_fails_clearly_if_incompatible(broken: str) -> None:
    px = pytest.importorskip("pinecone")
    index = px.Index(api_key="test-key", host="https://idx.svc.pinecone.io")
    original_http, original_config = index._http, index._config
    version = original_http._headers["X-Pinecone-Api-Version"]
    try:
        if broken == "http":
            index._http = object()
        elif broken == "config":
            index._config = object()
        else:
            del original_http._headers["X-Pinecone-Api-Version"]
        with pytest.raises(StoreError, match="incompatible SDK: cannot disable index retries"):
            _disable_index_retries(index, px)
        assert not original_http._client.is_closed
    finally:
        index._http, index._config = original_http, original_config
        original_http._headers["X-Pinecone-Api-Version"] = version
        index.close()


# ---- the contract suite: shared parts --------------------------------------------------------------


class Tracked:
    """Wraps a store and remembers which ids each namespace should hold, so ``settle`` can wait for the
    eventually consistent ``describe_index_stats`` count to catch up (the intent is recorded *before* the
    write, so concurrent writers converge too)."""

    def __init__(self, inner: PineconeStore) -> None:
        self.inner = inner
        self.expected: dict[str, set[str]] = {}
        self.last: dict[str, list[tuple[str, dict[str, Any]]]] = {}  # the most recent upsert per namespace
        self.lock = threading.Lock()

    def __getattr__(self, attr: str) -> Any:
        return getattr(self.inner, attr)

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        with self.lock:
            self.expected.setdefault(name, set()).update(r.id for r in records)
            if records:
                self.last[name] = [(r.id, dict(r.metadata)) for r in records[-50:]]
        self.inner.upsert(name, records)

    def delete(self, name: str, ids: Sequence[str]) -> None:
        with self.lock:
            self.expected.setdefault(name, set()).difference_update(ids)
        self.inner.delete(name, ids)

    def drop_collection(self, name: str) -> None:
        with self.lock:
            self.expected.pop(name, None)
            self.last.pop(name, None)
        self.inner.drop_collection(name)

    def wait(self, name: str, timeout: float = 30.0) -> None:
        index, _ = self.inner._resolve()
        ns = self.inner._prefix + name

        def visible() -> bool:
            stats = index.describe_index_stats()
            with self.lock:
                want = len(self.expected.get(name, ()))
                recent = [(i, md) for i, md in self.last.get(name, []) if i in self.expected.get(name, ())]
            if int(getattr(stats.namespaces.get(ns), "vector_count", 0) or 0) != want:
                return False
            if not recent:
                return True
            # An overwrite does not change the count, so also see the newest writes (metadata included).
            got = index.fetch(ids=[i for i, _ in recent], namespace=ns).vectors
            return all(i in got and dict(got[i].metadata or {}) == md for i, md in recent)

        wait_until(visible, timeout=timeout, what=f"namespace {ns} to show its latest writes")


class PineconeContract(VectorStoreContract):
    """The contract, adapted to what Pinecone is: one index = one dimension and one metric.

    ``make_store(metric, dim)`` returns a store on a fresh index. Two contract tests assume a collection can be
    re-created with another dimension, or that one store serves every metric; for Pinecone those become
    "a different dimension or metric is a CollectionMismatch" and "one store per metric".
    """

    def make_store(self, metric: str, dim: int = DIM) -> Tracked:  # pragma: no cover - overridden
        raise NotImplementedError

    def settle(self, store: VectorStore, name: str) -> None:
        assert isinstance(store, Tracked)
        store.wait(name)

    def test_query_ordering_per_metric(self, store: VectorStore) -> None:  # overrides the contract test
        for i, metric in enumerate(["cosine", "l2", "dot"]):
            per = self.make_store(metric)
            name = f"rc_metric_{i:04d}"
            try:
                records = self._loaded(per, metric, name=name)  # type: ignore[arg-type]
                rng = random.Random(SEED + i)
                for _ in range(10):
                    q = random_vector(rng, DIM)
                    got = per.query(name, q, 10)
                    self._check(per, got, records, q, 10, metric, None)  # type: ignore[arg-type]
            finally:
                self.discard(per)

    def test_drop_existing_and_missing(self, store: VectorStore) -> None:  # overrides the contract test
        records = self._loaded(store, "cosine")
        store.drop_collection("rc_contract_0001")
        self.settle(store, "rc_contract_0001")
        assert store.collection_info("rc_contract_0001") is None
        store.drop_collection("rc_contract_0001")
        store.drop_collection("rc_never_existed")
        self.settle(store, "rc_never_existed")
        with pytest.raises(CollectionMismatch):  # the index fixes the dimension: it cannot change after a drop
            store.ensure_collection(self.spec(dim=DIM + 2, metric="cosine"))
        self._ensure(store, self.spec(metric="cosine"))
        info = store.collection_info("rc_contract_0001")
        assert info is not None and (info.dim, info.count) == (DIM, 0)
        got = store.query("rc_contract_0001", records[0].vector, 5)
        assert got == []  # nothing survives the drop

    def discard(self, store: Any) -> None:
        store.close()

    def test_drop_does_not_touch_other_namespaces(self, store: VectorStore) -> None:
        a = self._loaded(store, "cosine", name="rc_keep_0001")
        self._loaded(store, "cosine", name="rc_gone_0001")
        store.drop_collection("rc_gone_0001")
        self.settle(store, "rc_gone_0001")
        info = store.collection_info("rc_keep_0001")
        assert info is not None and info.count == N_RECORDS
        got = store.query("rc_keep_0001", a[0].vector, 1)
        assert [m.id for m in got] == [a[0].id]

    def test_collections_do_not_see_each_other(self, store: VectorStore) -> None:
        records = make_records(random.Random(11), 30)
        self._ensure(store, self.spec(name="rc_aaa_0001"))
        self._ensure(store, self.spec(name="rc_bbb_0001"))
        self._put(store, "rc_aaa_0001", records)
        assert store.query("rc_bbb_0001", records[0].vector, 5) == []
        assert len(store.query("rc_aaa_0001", records[0].vector, 5)) == 5


# ---- the contract suite against the fake client -----------------------------------------------------


class TestPineconeFakeContract(PineconeContract):
    stores: list[PineconeStore]

    @pytest.fixture(autouse=True)
    def _tracking(self, tmp_path: Path) -> Iterator[None]:
        self.tmp = tmp_path
        self.stores = []
        yield
        for s in self.stores:
            s.close()

    def make_store(self, metric: str, dim: int = DIM) -> Tracked:
        n = len(self.stores)
        store, _ = make_fake_store(self.tmp, dim=dim, metric=PMETRIC[metric], delay=0.03, prefix="t_", name=f"i{n}")
        self.stores.append(store)
        return Tracked(store)

    @pytest.fixture
    def store(self, _tracking: None) -> Tracked:
        return self.make_store("cosine")

    def discard(self, store: Any) -> None:
        pass  # closed with the others


# ---- live: Pinecone Local --------------------------------------------------------------------------


def _local_up() -> bool:
    try:
        pytest.importorskip("pinecone")
        return httpx.get(f"{LOCAL_URL}/indexes", headers=LOCAL_HEADERS, timeout=2).status_code == 200
    except (httpx.HTTPError, OSError):
        return False


@pytest.fixture(scope="module")
def local_available() -> None:
    if not _local_up():
        pytest.skip(
            f"no Pinecone Local at {LOCAL_URL} (set RAGCENTRE_TEST_PINECONE_URL) or the pinecone extra is missing"
        )


def _create_local_index(metric: str, dim: int) -> str:
    name = f"rct-{uuid.uuid4().hex[:12]}"
    body = {
        "name": name,
        "dimension": dim,
        "metric": PMETRIC[metric],
        "spec": {"serverless": {"cloud": "aws", "region": "us-east-1"}},
    }
    reply = httpx.post(f"{LOCAL_URL}/indexes", headers=LOCAL_HEADERS, json=body, timeout=30)
    if reply.status_code >= 300:
        pytest.skip(
            f"Pinecone Local would not create an index (out of ports?): HTTP {reply.status_code} {reply.text[:100]}"
        )
    return name


def _delete_local_index(name: str) -> None:
    with contextlib.suppress(httpx.HTTPError):
        httpx.delete(f"{LOCAL_URL}/indexes/{name}", headers=LOCAL_HEADERS, timeout=30)


@pytest.mark.integration
@pytest.mark.usefixtures("local_available")
class TestPineconeLocalContract(PineconeContract):
    """The whole contract against the emulator. Pinecone Local answered every filtered query exactly
    (that is evidence about the emulator only, so the adapter still declares ``filter_exact=False``);
    this class therefore checks filtered queries for the exact top-k, not just recall."""

    @pytest.fixture(autouse=True)
    def _tracking(self, tmp_path: Path) -> Iterator[None]:
        self.tmp = tmp_path
        self.opened: list[Tracked] = []
        self.indexes: list[str] = []
        yield
        for tracked in self.opened:
            tracked.close()
        for index in self.indexes:
            _delete_local_index(index)

    def make_store(self, metric: str, dim: int = DIM) -> Tracked:
        index = _create_local_index(metric, dim)
        self.indexes.append(index)  # recorded first, so a failed connect still deletes the index
        inner = PineconeStore.connect(index_name=index, data_dir=self.tmp, prefix="rct_", controller_host=LOCAL_URL)
        tracked = Tracked(inner)
        self.opened.append(tracked)
        return tracked

    @pytest.fixture
    def store(self, _tracking: None) -> Tracked:
        return self.make_store("cosine")

    def discard(self, store: Any) -> None:
        pass  # closed and deleted by the fixture

    def _check(self, store: VectorStore, got: list[Match], records: Sequence[Record], q: Sequence[float], k: int,
               metric: Metric, f: Filter | None) -> None:  # fmt: skip
        assert_same_results(got, records, q, k, metric, f, self.score_tolerance)

    def test_selective_filter_is_exact_on_the_emulator(self, store: VectorStore) -> None:
        rng = random.Random(SEED)
        records = [
            Record(r.id, r.vector, {**r.metadata, "lang": "rare"}) if i % 50 == 0 else r
            for i, r in enumerate(make_records(rng))
        ]
        self._ensure(store, self.spec(metric="cosine"))
        self._put(store, "rc_contract_0001", records)
        f = parse_filter({"lang": "rare"}, full_schema())
        for k in (2, 10):
            assert len(store.query("rc_contract_0001", random_vector(rng, DIM), k, f)) == min(k, 4)

    def test_index_checks_use_the_real_description(self, store: VectorStore) -> None:
        assert isinstance(store, Tracked)
        shape = store.inner._resolve(refresh=True)[1]
        assert (shape.dim, shape.metric) == (DIM, "cosine")
        store.healthcheck()
        with pytest.raises(CollectionMismatch, match=store.inner._index_name):
            store.ensure_collection(self.spec(dim=DIM + 1))
        with pytest.raises(CollectionMismatch, match=store.inner._index_name):
            store.ensure_collection(self.spec(metric="dot"))

    def test_missing_index_on_the_emulator(self, tmp_path: Path) -> None:
        store = PineconeStore.connect(index_name="rct-does-not-exist", data_dir=tmp_path, controller_host=LOCAL_URL)
        try:
            with pytest.raises(StoreError, match="never creates Pinecone indexes"):
                store.ensure_collection(self.spec())
        finally:
            store.close()

    def test_catalog_prefix_isolation_and_key_rotation_on_the_emulator(self, tmp_path: Path) -> None:
        index = _create_local_index("cosine", DIM)
        self.indexes.append(index)

        def connect(prefix: str, key: str = "pinecone-local") -> PineconeStore:
            store = PineconeStore.connect(
                index_name=index,
                data_dir=tmp_path,
                prefix=prefix,
                api_key=key,
                controller_host=LOCAL_URL,
            )
            self.opened.append(Tracked(store))
            assert store._shape is None
            return store

        name = "rc_unit_0001"
        a, b = connect("a_"), connect("b_")
        a.ensure_collection(_spec(fields={"lang": "str"}))
        a.upsert(name, [_rec()])
        wait_until(lambda: a.collection_info(name).count == 1, timeout=10)
        assert b.collection_info(name) is None
        b.ensure_collection(_spec(fields={"lang": "int"}))
        b.upsert(name, [_rec(lang=7)])
        wait_until(lambda: b.collection_info(name).count == 1, timeout=10)

        rotated = connect("a_", "rotated-local-key")
        assert rotated.collection_info(name).count == 1  # first operation reads the persisted catalog
        assert rotated._catalog.instance_key == a._catalog.instance_key
        assert rotated._catalog.get(name).fields == {"lang": "str"}
        assert rotated.query(name, _rec().vector, 1)[0].id == "r1"
        b.drop_collection(name)
        assert b.collection_info(name) is None
        assert rotated.collection_info(name).count == 1
        deleting = connect("a_", "another-local-key")
        deleting.delete(name, ["r1"])  # first operation must resolve before looking in the catalog
        wait_until(lambda: deleting.collection_info(name).count == 0, timeout=10)
        assert a.collection_info(name).count == 0

    def test_euclidean_scores_are_the_negative_distance(self, tmp_path: Path) -> None:
        index = _create_local_index("l2", 3)
        store = PineconeStore.connect(index_name=index, data_dir=tmp_path, controller_host=LOCAL_URL)
        try:
            spec = CollectionSpec("rc_l2_0001", 3, "l2", {})
            store.ensure_collection(spec)
            vecs = {"a": [1.0, 2.0, 3.0], "b": [3.0, 2.0, 1.0], "c": [0.0, 0.0, 5.0]}
            store.upsert("rc_l2_0001", [Record(i, v, {}) for i, v in vecs.items()])
            wait_until(lambda: (store.collection_info("rc_l2_0001") or SimpleNamespace(count=0)).count == 3, timeout=10)
            got = store.query("rc_l2_0001", [1.0, 0.0, 0.0], 3)
            assert [(m.id, round(m.score, 5)) for m in got] == [
                ("b", -3.0), ("a", round(-math.sqrt(13.0), 5)), ("c", round(-math.sqrt(26.0), 5))
            ]  # fmt: skip
            assert brute_score("l2", vecs["b"], [1.0, 0.0, 0.0]) == pytest.approx(-3.0)
        finally:
            store.close()
            _delete_local_index(index)


# ---- live: hosted Pinecone (opt-in, billed) ---------------------------------------------------------


def _cloud_client() -> Any:
    px = pytest.importorskip("pinecone")
    return px.Pinecone(api_key=CLOUD_KEY)


@pytest.mark.cloud
@pytest.mark.integration
@pytest.mark.skipif(
    not CLOUD_KEY, reason="RAGCENTRE_TEST_PINECONE_API_KEY is not set (hosted Pinecone tests are opt-in)"
)
class TestPineconeCloudContract(PineconeContract):
    """The contract against a real serverless index. NOT RUN in the session that wrote it (no API key was
    available): the setup below follows the pinecone 10.3.0 ``indexes.create`` signature as read from the source.
    Set ``RAGCENTRE_TEST_PINECONE_API_KEY`` to run it; it creates ``rct-*`` indexes and deletes them afterwards."""

    @pytest.fixture(autouse=True)
    def _tracking(self, tmp_path: Path) -> Iterator[None]:
        self.tmp = tmp_path
        self.pc = _cloud_client()
        self.opened: list[Tracked] = []
        self.indexes: list[str] = []
        yield
        for tracked in self.opened:
            tracked.close()
        for index in self.indexes:
            with contextlib.suppress(Exception):
                self.pc.indexes.delete(index)
        self.pc.close()

    def make_store(self, metric: str, dim: int = DIM) -> Tracked:
        name = f"rct-{uuid.uuid4().hex[:12]}"
        self.indexes.append(name)
        self.pc.indexes.create(
            name=name,
            dimension=dim,
            metric=PMETRIC[metric],
            deployment={
                "deployment_type": "managed",
                "cloud": os.environ.get("RAGCENTRE_TEST_PINECONE_CLOUD", "aws"),
                "region": os.environ.get("RAGCENTRE_TEST_PINECONE_REGION", "us-east-1"),
            },
        )
        inner = PineconeStore.connect(index_name=name, data_dir=self.tmp, api_key=CLOUD_KEY, prefix="rct_")
        tracked = Tracked(inner)
        self.opened.append(tracked)
        return tracked

    @pytest.fixture
    def store(self, _tracking: None) -> Tracked:
        return self.make_store("cosine")

    def discard(self, store: Any) -> None:
        pass
