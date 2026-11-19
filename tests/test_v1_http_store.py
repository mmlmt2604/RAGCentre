"""The `http` vector store and the reference memory shim, tested together.

The contract suite runs against ``stores/http.py`` pointed at the in-process shim (an ASGI app
served through ``fastapi.testclient.TestClient``), which exercises the protocol and the client in
one go. Additional tests cover the error envelope, authentication, and the "retry writes, never
queries" rule at the client level.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ragcentre.errors import CollectionMismatch
from ragcentre.stores.base import CollectionNotFound, CollectionSpec, StoreError
from ragcentre.stores.http import HttpStore
from ragcentre.testing.random_data import SCHEMA_FIELDS
from ragcentre.testing.vector_store_contract import VectorStoreContract

SHIM_DIR = Path(__file__).resolve().parents[1] / "examples" / "vector_store_shims" / "memory"

INFO_BODY = {
    "protocol": "ragcentre.vector_store/v1",
    "service_name": "memory-shim",
    "service_version": "0.1.0",
    "capabilities": {"metrics": ["cosine", "l2", "dot"], "max_batch": 1000},
}


def _create_app(**kwargs: Any) -> Any:
    sys.path.insert(0, str(SHIM_DIR))
    import memory_shim

    return memory_shim.create_app(**kwargs)


class TestHttpStore(VectorStoreContract):
    """The full contract suite, run over HTTP against the in-process reference shim."""

    @pytest.fixture
    def store(self) -> Iterator[HttpStore]:
        client = TestClient(_create_app())
        store = HttpStore.connect("http://testserver", client=client)
        yield store
        store.close()


class TestHttpStoreWithLimits(VectorStoreContract):
    """A service that advertises ``max_dim`` and a small ``max_batch``: both are captured and enforced."""

    @pytest.fixture
    def store(self) -> Iterator[HttpStore]:
        client = TestClient(_create_app(max_dim=8, max_batch=50))
        store = HttpStore.connect("http://testserver", client=client)
        assert store.capabilities.max_dim == 8
        assert store.capabilities.max_batch == 50
        yield store
        store.close()


# ---- error paths -----------------------------------------------------------------------------


def test_error_paths_against_shim() -> None:
    client = TestClient(_create_app())
    store = HttpStore.connect("http://testserver", client=client)
    try:
        # 404 -> collection_info returns None; query raises CollectionNotFound.
        assert store.collection_info("rc_missing_0001") is None
        with pytest.raises(CollectionNotFound):
            store.query("rc_missing_0001", [0.0] * 8, 5)
        # 409 -> CollectionMismatch on a changed dimension.
        store.ensure_collection(CollectionSpec("rc_err_0001", 8, "cosine", dict(SCHEMA_FIELDS)))
        with pytest.raises(CollectionMismatch):
            store.ensure_collection(CollectionSpec("rc_err_0001", 9, "cosine", dict(SCHEMA_FIELDS)))
        # 400 invalid_request -> StoreError on a wrong query dimension.
        with pytest.raises(StoreError, match=r"invalid_request|dimension"):
            store.query("rc_err_0001", [0.0] * 9, 5)
    finally:
        store.close()


def test_shim_error_envelopes() -> None:
    client = TestClient(_create_app())
    try:
        missing = client.get("/collections/rc_missing_0001")
        assert missing.status_code == 404
        assert missing.json() == {
            "error": {"code": "collection_not_found", "message": "collection rc_missing_0001 does not exist"}
        }

        client.put("/collections/rc_env_0001", json={"dim": 8, "metric": "cosine", "fields": {}})
        mismatch = client.put("/collections/rc_env_0001", json={"dim": 9, "metric": "cosine", "fields": {}})
        assert mismatch.status_code == 409
        assert mismatch.json()["error"]["code"] == "collection_mismatch"

        bad = client.post("/collections/rc_env_0001/query", json={"vector": [0.0] * 9, "k": 5, "filter": None})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "invalid_request"
    finally:
        client.close()


def test_info_is_open_and_health_is_protected() -> None:
    client = TestClient(_create_app(api_key="sekret"))
    try:
        assert client.get("/info").status_code == 200
        assert client.get("/health").status_code == 401
        assert client.get("/health").json()["error"]["code"] == "unauthorized"
        client.headers["Authorization"] = "Bearer sekret"
        assert client.get("/health").status_code == 200
    finally:
        client.close()


# ---- retry behaviour (client side, no shim) --------------------------------------------------


def test_writes_retry_queries_do_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ragcentre.stores.http._sleep", lambda _seconds: None)
    counts = {"put": 0, "query": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json=INFO_BODY)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "PUT":
            counts["put"] += 1
            if counts["put"] < 3:
                return httpx.Response(503, json={"error": {"code": "unavailable", "message": "warming up"}})
            return httpx.Response(200, json={})
        if request.url.path.endswith("/query"):
            counts["query"] += 1
            return httpx.Response(503, json={"error": {"code": "unavailable", "message": "down"}})
        return httpx.Response(404, json={"error": {"code": "collection_not_found", "message": "nope"}})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    store = HttpStore.connect("http://test", client=client)
    try:
        store.ensure_collection(CollectionSpec("rc_retry_0001", 8, "cosine", {}))
        assert counts["put"] == 3  # 503, 503, then success
        with pytest.raises(StoreError):
            store.query("rc_retry_0001", [0.0] * 8, 5)
        assert counts["query"] == 1  # a failing read is never retried
    finally:
        store.close()


def test_writes_do_not_retry_permanent_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ragcentre.stores.http._sleep", lambda _seconds: None)
    counts = {"put": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json=INFO_BODY)
        counts["put"] += 1
        return httpx.Response(400, json={"error": {"code": "invalid_request", "message": "bad request"}})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    store = HttpStore.connect("http://test", client=client)
    try:
        with pytest.raises(StoreError):
            store.ensure_collection(CollectionSpec("rc_perm_0001", 8, "cosine", {}))
        assert counts["put"] == 1  # a 4xx is permanent, even on a write
    finally:
        store.close()


# ---- registration and /info validation --------------------------------------------------------


def test_factory_and_type(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ragcentre.stores import http as http_module

    app = _create_app(api_key="k")
    monkeypatch.setattr(http_module.httpx, "Client", lambda **kw: TestClient(app, headers=kw.get("headers")))
    spec = http_module.STORE_TYPE
    assert spec.type == "http" and spec.secret_keys == ("api_key",)
    config = spec.config_model.model_validate({"url": "http://svc:1", "timeout": 5})
    store = spec.factory(config, {"api_key": "k"}, tmp_path)
    try:
        store.healthcheck()  # the bearer token reached the service
        assert store.capabilities.metrics == {"cosine", "l2", "dot"}  # type: ignore[attr-defined]
    finally:
        store.close()
    with pytest.raises(Exception, match=r"extra|forbid|Extra"):
        spec.config_model.model_validate({"url": "http://svc:1", "bogus": 1})


@pytest.mark.parametrize(
    "info",
    [
        {**INFO_BODY, "protocol": "ragcentre.parser/v1"},
        {**INFO_BODY, "service_version": ""},
        {**INFO_BODY, "capabilities": {"metrics": [], "max_batch": 10}},
        {**INFO_BODY, "capabilities": {"metrics": ["cosine"], "max_batch": 0}},
        {**INFO_BODY, "capabilities": {"metrics": ["manhattan"], "max_batch": 10}},
    ],
)
def test_bad_info_is_rejected(info: dict[str, Any]) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=info)), base_url="http://t")
    with pytest.raises(StoreError):
        HttpStore.connect("http://t", client=client)


def test_info_is_read_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from ragcentre.stores import http as http_module

    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/info":
            return httpx.Response(200, json=INFO_BODY)
        return httpx.Response(200, json={"status": "ok"})

    real_client = httpx.Client
    monkeypatch.setattr(
        http_module.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    store = HttpStore.connect("http://t", api_key="sekret")
    try:
        store.healthcheck()
    finally:
        store.close()
    assert seen == [("/info", None), ("/health", "Bearer sekret")]


def test_memory_shim_module_exposes_app() -> None:
    sys.path.insert(0, str(SHIM_DIR))
    import memory_shim

    assert memory_shim.app.title.startswith("memory-shim")  # what `uvicorn memory_shim:app` loads
