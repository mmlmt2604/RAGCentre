from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from ragcentre.stores.base import StoreError
from ragcentre.stores.http import HttpStore
from ragcentre.testing.vector_store_contract import VectorStoreContract

pytestmark = pytest.mark.integration


@pytest.fixture
def shim_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    shim_dir = Path(__file__).resolve().parents[1] / "examples/vector_store_shims/memory"
    monkeypatch.syspath_prepend(str(shim_dir))
    from memory_shim import create_app

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(create_app(api_key="network-test-key"), log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive(), "HTTP shim exited before startup"
                assert time.monotonic() < deadline, "HTTP shim did not start"
                time.sleep(0.01)
            yield url
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "HTTP shim did not stop"


class TestHttpStoreNetwork(VectorStoreContract):
    @pytest.fixture
    def store(self, shim_url: str) -> Iterator[HttpStore]:
        store = HttpStore.connect(shim_url, api_key="network-test-key")
        yield store
        store.close()


def test_info_is_public_but_data_access_requires_auth(shim_url: str) -> None:
    with httpx.Client(base_url=shim_url) as client:
        assert client.get("/info").status_code == 200
        assert client.get("/health").status_code == 401
    store = HttpStore.connect(shim_url, api_key="wrong-key")
    try:
        with pytest.raises(StoreError, match="401"):
            store.healthcheck()
    finally:
        store.close()
