"""OpenSearch vector store (optional extra ``ragcentre[opensearch]``).

As the Elasticsearch store, with an OpenSearch ``knn_vector`` field and the index setting
``index.knn: true``. The k-NN ``method`` is HNSW on the ``faiss`` (default) or ``lucene`` engine, an
immutable option; ``nmslib`` is never used, because it has no efficient filtering (it post-filters,
which can return fewer than ``k`` matches) and is deprecated.

**Search.** ``knn`` query with the filter inside the ``knn`` clause (efficient filtering: HNSW on
both engines from 2.9). Faiss searches ``ef_search`` candidates (default 100), so a larger ``k`` can
under-fill; the adapter passes ``method_parameters.ef_search = k * num_candidates_factor`` (at least
``k``, capped at 10,000), which fixed it in the live check. The lucene engine returned every document
for ``k`` equal to the collection size without it.

**Scores.** Both engines return the Lucene-style transformed score, inverted per metric (see
``_lucene_scores``): ``cosinesimil`` ``(1+cos)/2``, ``l2`` ``1/(1+d^2)`` and ``innerproduct``
``dot+1`` or ``1/(1-dot)`` (verified numerically on OpenSearch 3.10.0 for all six engine/space pairs).

**Dimensions.** 16,000 on both engines (verified: 16,001 is rejected).

**Versions.** Verified on OpenSearch 3.10.0 with opensearch-py 3.2.0. The adapter requires 2.19 or
later for faiss (``cosinesimil`` on faiss and ``method_parameters`` arrived by then) and 2.13 for
lucene (``innerproduct``); older servers are read about, not run.

``opensearchpy`` is imported only when a store is created, so ``import ragcentre`` works without
the extra.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ragcentre.stores._lucene_scores import (
    VECTOR_FIELD,
    SearchEngineStore,
    check_index_prefix,
    error_status,
)
from ragcentre.stores.base import Capabilities, Metric, StoreError, require_extra
from ragcentre.stores.catalog import CatalogEntry
from ragcentre.stores.registry import VectorStoreType

Engine = Literal["faiss", "lucene"]
SPACE: dict[Metric, str] = {"cosine": "cosinesimil", "l2": "l2", "dot": "innerproduct"}
MIN_VERSION: dict[str, tuple[int, ...]] = {"faiss": (2, 19), "lucene": (2, 13)}

# Both engines cap knn_vector at 16,000 dimensions (verified on 3.10.0).
CAPABILITIES = Capabilities(metrics=frozenset({"cosine", "l2", "dot"}), max_batch=500, max_dim=16_000)


def _import() -> Any:
    return require_extra("opensearchpy", store="opensearch", extra="opensearch", package="opensearch-py")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


class OpenSearchStore(SearchEngineStore):
    label = "opensearch"
    capabilities = CAPABILITIES

    def __init__(
        self, client: Any, *, prefix: str = "", num_candidates_factor: int = 10, engine: Engine = "faiss"
    ) -> None:
        if engine not in ("faiss", "lucene"):  # never nmslib
            raise StoreError(f"opensearch: engine must be 'faiss' or 'lucene', not {engine!r}")
        super().__init__(client, prefix=prefix, num_candidates_factor=num_candidates_factor, options={"engine": engine})
        self._engine: Engine = engine
        self._server_version: tuple[int, ...] | None = None

    @classmethod
    def connect(
        cls,
        *,
        url: str,
        username: str | None = None,
        password: str | None = None,
        verify_certs: bool = True,
        ca_certs: str | None = None,
        request_timeout: float = 30.0,
        prefix: str = "",
        num_candidates_factor: int = 10,
        engine: Engine = "faiss",
    ) -> OpenSearchStore:
        mod = _import()
        kwargs: dict[str, Any] = {
            "hosts": [url],
            "use_ssl": url.lower().startswith("https://"),
            "verify_certs": verify_certs,
            "timeout": request_timeout,
            "max_retries": 0,
            "retry_on_timeout": False,
        }
        if ca_certs:
            kwargs["ca_certs"] = ca_certs
        if password:
            kwargs["http_auth"] = (username or "admin", password)
        try:
            client = mod.OpenSearch(**kwargs)
        except Exception as exc:
            raise StoreError(f"opensearch: could not create a client: {exc}") from exc
        return cls(client, prefix=prefix, num_candidates_factor=num_candidates_factor, engine=engine)

    # ---- engine hooks -------------------------------------------------------------------------

    def _info(self) -> dict[str, Any]:
        return dict(self._client.info())

    def _get_mappings(self, index: str) -> dict[str, Any] | None:
        try:
            body = self._client.indices.get_mapping(index=index)
        except Exception as exc:
            if error_status(exc) == 404:
                return None
            raise
        return dict(body[index]["mappings"])

    def _create_index(self, index: str, mappings: dict[str, Any]) -> None:
        self._client.indices.create(index=index, body={"settings": {"index": {"knn": True}}, "mappings": mappings})

    def _put_mappings(self, index: str, mappings: dict[str, Any]) -> None:
        self._client.indices.put_mapping(index=index, body=mappings)

    def _delete_index(self, index: str) -> None:
        self._client.indices.delete(index=index, ignore_unavailable=True)

    def _bulk(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(self._client.bulk(body=operations, refresh="wait_for"))

    def _count(self, index: str) -> int:
        return int(self._client.count(index=index)["count"])

    def _vector_mapping(self, dim: int, metric: Metric) -> dict[str, Any]:
        return {
            "type": "knn_vector",
            "dimension": dim,
            "method": {"name": "hnsw", "engine": self._engine, "space_type": SPACE[metric]},
        }

    def _check_server(self, entry_options: Mapping[str, Any], metric: Metric) -> None:
        if self._server_version is None:
            self._server_version = _version(str(self._info()["version"]["number"]))
        needed = MIN_VERSION[self._engine]
        if self._server_version < needed:
            raise StoreError(
                f"opensearch {'.'.join(map(str, self._server_version))} is too old for the {self._engine} engine; "
                f"it needs {'.'.join(map(str, needed))} or later"
            )

    def _knn(self, index: str, entry: CatalogEntry, vector: Sequence[float], k: int, flt: dict[str, Any] | None) -> Any:
        clause: dict[str, Any] = {"vector": [float(x) for x in vector], "k": k}
        if flt is not None:
            clause["filter"] = flt  # inside the knn clause: efficient filtering, not a post-filter
        if entry.options.get("engine", self._engine) == "faiss":
            clause["method_parameters"] = {"ef_search": self.num_candidates(k)}
        body = {"size": k, "_source": False, "query": {"knn": {VECTOR_FIELD: clause}}}
        return self._client.search(index=index, body=body)


# ---- registration ---------------------------------------------------------------------------


class OpenSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(description="OpenSearch URL, e.g. https://localhost:9200")
    username: str | None = Field(default=None, description="User for password authentication (default 'admin')")
    verify_certs: bool = Field(default=True, description="Verify the server's TLS certificate")
    ca_certs: str | None = Field(default=None, description="Path to a CA bundle for a private certificate authority")
    request_timeout: float = Field(default=30.0, gt=0, description="Seconds before a request times out")
    engine: Engine = Field(
        default="faiss",
        description="k-NN engine (set once): faiss or lucene. Both filter inside the search; nmslib is never used",
    )
    num_candidates_factor: int = Field(
        default=10, ge=1, le=1000, description="Faiss ef_search = k x this factor (capped at 10,000): recall vs speed"
    )
    prefix: str = Field(default="", description="Prefix prepended to the OpenSearch index name")

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        return check_index_prefix(v)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        if not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("url must start with http:// or https://")
        return v


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> OpenSearchStore:
    assert isinstance(config, OpenSearchConfig)
    return OpenSearchStore.connect(
        url=config.url,
        username=config.username,
        password=secrets.get("password") or None,
        verify_certs=config.verify_certs,
        ca_certs=config.ca_certs,
        request_timeout=config.request_timeout,
        prefix=config.prefix,
        num_candidates_factor=config.num_candidates_factor,
        engine=config.engine,
    )


STORE_TYPE = VectorStoreType(
    type="opensearch",
    label="OpenSearch",
    description="OpenSearch 2.19+ k-NN (faiss or lucene HNSW). Needs the optional 'opensearch' extra.",
    config_model=OpenSearchConfig,
    secret_keys=("password",),
    capabilities=CAPABILITIES,
    factory=_factory,
    immutable_fields=("engine",),
)
