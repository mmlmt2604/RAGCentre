from collections.abc import Iterator
from pathlib import Path

import pytest

from ragcentre.stores.base import VectorStore
from ragcentre.testing.vector_store_contract import VectorStoreContract
from vector_store_example import STORE_TYPE, ExampleStore


class TestExampleAdapter(VectorStoreContract):
    @pytest.fixture
    def store(self) -> Iterator[ExampleStore]:
        store = ExampleStore()
        yield store
        store.close()


def test_example_factory(tmp_path: Path) -> None:
    store = STORE_TYPE.factory(STORE_TYPE.config_model.model_validate({"max_dim": 256}), {}, tmp_path)
    try:
        assert isinstance(store, VectorStore)
        assert store.capabilities.max_dim == 256
        assert STORE_TYPE.capabilities.max_dim is None  # type best case, separate from instance
        store.healthcheck()
    finally:
        store.close()
