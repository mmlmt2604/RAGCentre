from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ragcentre.context import AppContext
from ragcentre.errors import ExecutionError
from ragcentre.hashing import new_id
from ragcentre.pipeline.executors import execute
from ragcentre.pipeline.planner import create_pipeline
from ragcentre.providers import registry
from ragcentre.providers.manager import fingerprint_for
from ragcentre.queue.claims import claim_run, complete
from ragcentre.stores.base import (
    Capabilities,
    CollectionSpec,
    DimensionLimitExceeded,
    StoreError,
    VectorStore,
    check_spec,
)
from ragcentre.stores.catalog import InStoreCatalog
from ragcentre.stores.registry import VectorStoreType
from ragcentre.stores.sqlite_vec import SqliteVecStore
from test_m6_ingestion import MD, _add_providers, _corpus, _insert_doc, _pipeline
from vs_support import FakeStoreConfig, MemoryStore

CAPS = Capabilities(frozenset({"cosine"}), 50)


def test_old_capabilities_constructor_and_new_defaults() -> None:
    assert asdict(CAPS) == {
        "metrics": frozenset({"cosine"}),
        "max_batch": 50,
        "multi_vector": False,
        "native_hybrid": False,
        "max_dim": None,
        "consistency": "immediate",
        "filter_exact": True,
        "max_collections": None,
    }
    assert Capabilities(CAPS.metrics, 50, True, True).native_hybrid
    check_spec(CollectionSpec("rc_old_001", 50000, "cosine"), CAPS)


def test_instance_and_class_capabilities_satisfy_protocol(tmp_path: Path) -> None:
    texts: dict[str, str] = {}
    instance_store: VectorStore = MemoryStore(InStoreCatalog(texts.get, texts.__setitem__))
    class_store: VectorStore = SqliteVecStore(tmp_path / "vectors.db")
    try:
        assert isinstance(instance_store, VectorStore)
        assert isinstance(class_store, VectorStore)
    finally:
        instance_store.close()
        class_store.close()


def test_dimension_limit_error_and_boundary() -> None:
    caps = replace(CAPS, max_dim=2000)
    check_spec(CollectionSpec("rc_limit_01", 2000, "cosine"), caps)
    with pytest.raises(DimensionLimitExceeded, match="needs 3072 dimensions; this store supports at most 2000") as err:
        check_spec(CollectionSpec("rc_limit_01", 3072, "cosine"), caps)
    assert isinstance(err.value, StoreError)
    with pytest.raises(StoreError, match="at least 1"):
        check_spec(CollectionSpec("rc_limit_01", 0, "cosine"), caps)


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> VectorStore:
    assert isinstance(config, FakeStoreConfig)
    texts: dict[str, str] = {}
    return MemoryStore(
        InStoreCatalog(texts.get, texts.__setitem__),
        capabilities=replace(CAPS, max_dim=config.max_dim),
        options={"quantization": config.quantization},
    )


FAKE = VectorStoreType(
    type="v1_fake",
    label="Fake store",
    description="Tests immutable options",
    config_model=FakeStoreConfig,
    secret_keys=(),
    capabilities=CAPS,
    factory=_factory,
    immutable_fields=("quantization",),
)


def test_store_immutable_config_returns_409_and_connections_stay_mutable(
    ctx: AppContext,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = registry._store_spec(FAKE)
    types = {**registry.provider_types(), spec.key: spec}
    monkeypatch.setattr(registry, "provider_types", lambda: types)
    result = client.post(
        "/v1/providers",
        json={
            "name": "fake-store",
            "kind": "vector_store",
            "type": "v1_fake",
            "config": {"quantization": "scalar"},
        },
    )
    assert result.status_code == 201, result.text
    row = result.json()
    url = f"/v1/providers/{row['id']}"
    refused = client.patch(url, json={"config": {"quantization": "none"}})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "immutable_field"
    assert refused.json()["error"]["details"] == {"field": "quantization"}
    accepted = client.patch(url, json={"config": {"quantization": "scalar", "url": "http://elsewhere:2"}})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["config"]["url"] == "http://elsewhere:2"
    assert accepted.json()["fingerprint"] == row["fingerprint"]
    # Store identity still determines the fingerprint, even when index options are permanent.
    assert fingerprint_for(spec, row["id"], {"quantization": "none"}) == row["fingerprint"]
    assert fingerprint_for(spec, "other-id", row["config"]) != row["fingerprint"]
    assert "quantization" not in spec.operational_fields()
    assert "url" in spec.operational_fields()
    # Legacy store types default to no immutable config fields.
    assert replace(FAKE, immutable_fields=()).immutable_fields == ()


def test_unknown_immutable_field_rejected() -> None:
    with pytest.raises(ValueError, match="not in its config model"):
        replace(FAKE, immutable_fields=("typo",))


@pytest.mark.parametrize("limit_error", [True, False], ids=["dimension_limit", "unrelated_store_error"])
def test_index_vector_classifies_only_dimension_limit(
    ctx: AppContext,
    monkeypatch: pytest.MonkeyPatch,
    limit_error: bool,
) -> None:
    _add_providers(ctx)
    corpus_id = _corpus(ctx)
    _insert_doc(ctx, corpus_id, MD, new_id())
    config = _pipeline()
    config["nodes"] = [n for n in config["nodes"] if n["id"] != "fts"]
    config["retrieval"]["sources"] = [config["retrieval"]["sources"][0]]
    config["retrieval"].pop("fusion")
    create_pipeline(ctx, corpus_id, config)
    # Complete the real parse/chunk/embed runs before exercising the index error path.
    for _ in range(3):
        claim = claim_run(ctx, "foundation")
        assert claim is not None
        complete(ctx, claim, execute(ctx, claim))
    claim = claim_run(ctx, "foundation")
    assert claim is not None
    store_row = ctx.providers.by_name("store")
    assert store_row is not None
    store = ctx.providers.store(store_row.id)

    def fail(spec: CollectionSpec) -> None:
        if limit_error:
            check_spec(spec, replace(store.capabilities, max_dim=1))
        raise StoreError("connection failed")

    monkeypatch.setattr(store, "ensure_collection", fail)
    if limit_error:
        with pytest.raises(ExecutionError) as error:
            execute(ctx, claim)
        assert error.value.code == "store_dimension_limit"
        assert "supports at most 1" in error.value.message
        assert not error.value.retryable
        assert isinstance(error.value.__cause__, DimensionLimitExceeded)
    else:
        with pytest.raises(StoreError, match="connection failed"):
            execute(ctx, claim)
    assert ctx.db.scalar("SELECT dim FROM indexes WHERE kind = 'vector'") is None
    assert ctx.db.scalar("SELECT COUNT(*) FROM index_records") == 0
