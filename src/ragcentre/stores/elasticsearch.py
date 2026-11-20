"""Elasticsearch vector store (optional extra ``ragcentre[elasticsearch]``).

One collection is one index named ``<prefix><collection>``: a ``dense_vector`` field, a ``document_id``
keyword and a ``keyword``/``long``/``double``/``boolean`` field per schema field, with
``dynamic: strict`` so nothing is mapped by accident. The catalog (dimension, metric, field types and the
index options) lives in the mapping's ``_meta`` and is written by the call that creates the index.

**Search.** A ``knn`` search with the filter *inside* the ``knn`` clause (a sibling ``bool.filter``
would be a post-filter and can return fewer than ``k`` matches). ``num_candidates`` is
``k * num_candidates_factor`` (at least ``k``, at most 10,000). The quantised ``index_options`` add
``rescore_vector`` so the returned scores are exact rather than quantised.

**Scores.** The ``_score`` of a k-NN hit is a transformed similarity (see ``_lucene_scores``); it is
inverted per metric. ``dot`` uses ``max_inner_product``, because ``dot_product`` accepts only unit
vectors. A zero vector has no cosine similarity: Elasticsearch rejects it, so the adapter does too.

**Writes.** Bulk requests with ``refresh=wait_for``, so a write is visible when ``upsert`` or
``delete`` returns: ``consistency`` is ``immediate``.

**Versions.** Verified against an Elasticsearch 9.5.1 server with the 9.5.1 client. In 9.x the
server's own default ``index_options`` is the quantised ``int8_hnsw`` (``bbq_hnsw`` from 384
dimensions), which costs recall, so the adapter defaults to plain ``hnsw`` and treats
``index_options`` as an immutable, index-shaping option. The 9.x client sends a ``compatible-with=9``
header that 8.x servers reject; for an 8.x server install the 8.x client (same calls).

``elasticsearch`` is imported only when a store is created, so ``import ragcentre`` works without
the extra.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ragcentre.stores._lucene_scores import (
    VECTOR_FIELD,
    SearchEngineStore,
    check_index_prefix,
    error_status,
)
from ragcentre.stores.base import Capabilities, Metric, StoreError, require_extra
from ragcentre.stores.catalog import CatalogEntry
from ragcentre.stores.registry import VectorStoreType

IndexOptions = Literal["hnsw", "int8_hnsw", "int4_hnsw", "bbq_hnsw", "flat", "int8_flat", "int4_flat", "bbq_flat"]
QUANTIZED: frozenset[str] = frozenset({"int8_hnsw", "int4_hnsw", "bbq_hnsw", "int8_flat", "int4_flat", "bbq_flat"})
SIMILARITY: dict[Metric, str] = {"cosine": "cosine", "l2": "l2_norm", "dot": "max_inner_product"}
RESCORE_OVERSAMPLE = 3.0
MIN_DIMS = {"bbq_hnsw": 64, "bbq_flat": 64}  # the server refuses fewer (verified on 9.5.1)
# First Elasticsearch release with each feature (read from the release docs, not run on old servers).
MIN_VERSION = (8, 11)  # max_inner_product and index_options.type
_MIN_OPTION_VERSION: dict[str, tuple[int, ...]] = {
    "int8_hnsw": (8, 12),
    "flat": (8, 13),
    "int8_flat": (8, 13),
    "int4_hnsw": (8, 15),
    "int4_flat": (8, 15),
    "bbq_hnsw": (8, 16),
    "bbq_flat": (8, 16),
}
RESCORE_VERSION = (8, 18)

# dense_vector holds at most 4096 dimensions for every index_options type (verified on 9.5.1).
CAPABILITIES = Capabilities(metrics=frozenset({"cosine", "l2", "dot"}), max_batch=500, max_dim=4096)


def _import() -> Any:
    return require_extra("elasticsearch", store="elasticsearch", extra="elasticsearch", package="elasticsearch")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


class ElasticsearchStore(SearchEngineStore):
    label = "elasticsearch"
    capabilities = CAPABILITIES

    def __init__(
        self, client: Any, *, prefix: str = "", num_candidates_factor: int = 10, index_options: IndexOptions = "hnsw"
    ) -> None:
        super().__init__(
            client,
            prefix=prefix,
            num_candidates_factor=num_candidates_factor,
            options={"index_options": index_options},
        )
        self._index_options = index_options
        self._server_version: tuple[int, ...] | None = None

    @classmethod
    def connect(
        cls,
        *,
        url: str | None = None,
        cloud_id: str | None = None,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        verify_certs: bool = True,
        ca_certs: str | None = None,
        request_timeout: float = 30.0,
        prefix: str = "",
        num_candidates_factor: int = 10,
        index_options: IndexOptions = "hnsw",
    ) -> ElasticsearchStore:
        es = _import()
        if (url is None) == (cloud_id is None):
            raise StoreError("elasticsearch needs exactly one of 'url' and 'cloud_id'")
        if api_key and password:
            raise StoreError("elasticsearch: give either an api_key or a password, not both")
        kwargs: dict[str, Any] = {"request_timeout": request_timeout, "verify_certs": verify_certs}
        if url is not None:
            kwargs["hosts"] = [url]
        else:
            kwargs["cloud_id"] = cloud_id
        if ca_certs:
            kwargs["ca_certs"] = ca_certs
        if api_key:
            kwargs["api_key"] = api_key
        elif password:
            kwargs["basic_auth"] = (username or "elastic", password)
        try:
            client = es.Elasticsearch(**kwargs)
        except Exception as exc:
            raise StoreError(f"elasticsearch: could not create a client: {exc}") from exc
        return cls(client, prefix=prefix, num_candidates_factor=num_candidates_factor, index_options=index_options)

    # ---- engine hooks -------------------------------------------------------------------------

    def _info(self) -> dict[str, Any]:
        return dict(self._client.info().body)

    def _get_mappings(self, index: str) -> dict[str, Any] | None:
        try:
            body = self._client.indices.get_mapping(index=index).body
        except Exception as exc:
            if error_status(exc) == 404:
                return None
            raise
        return dict(body[index]["mappings"])

    def _create_index(self, index: str, mappings: dict[str, Any]) -> None:
        self._client.indices.create(index=index, mappings=mappings)

    def _put_mappings(self, index: str, mappings: dict[str, Any]) -> None:
        self._client.indices.put_mapping(index=index, properties=mappings["properties"], meta=mappings["_meta"])

    def _delete_index(self, index: str) -> None:
        self._client.indices.delete(index=index, ignore_unavailable=True)

    def _bulk(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(self._client.bulk(operations=operations, refresh="wait_for").body)

    def _count(self, index: str) -> int:
        return int(self._client.count(index=index).body["count"])

    def _vector_mapping(self, dim: int, metric: Metric) -> dict[str, Any]:
        if dim < MIN_DIMS.get(self._index_options, 1):
            raise StoreError(
                f"elasticsearch: index_options {self._index_options!r} needs at least "
                f"{MIN_DIMS[self._index_options]} dimensions; the collection has {dim}"
            )
        return {
            "type": "dense_vector",
            "dims": dim,
            "index": True,
            "similarity": SIMILARITY[metric],
            "index_options": {"type": self._index_options},
        }

    def _supports_rescore(self) -> bool:
        if self._server_version is None:
            self._server_version = _version(str(self._info()["version"]["number"]))
        return self._server_version >= RESCORE_VERSION

    def _check_server(self, entry_options: Mapping[str, Any], metric: Metric) -> None:
        if self._server_version is None:
            self._server_version = _version(str(self._info()["version"]["number"]))
        version = self._server_version
        needed = max(MIN_VERSION, _MIN_OPTION_VERSION.get(self._index_options, MIN_VERSION))
        if version < needed:
            raise StoreError(
                f"elasticsearch {'.'.join(map(str, version))} is too old for index_options "
                f"{self._index_options!r}; it needs {'.'.join(map(str, needed))} or later"
            )

    def _knn(self, index: str, entry: CatalogEntry, vector: Sequence[float], k: int, flt: dict[str, Any] | None) -> Any:
        knn: dict[str, Any] = {
            "field": VECTOR_FIELD,
            "query_vector": [float(x) for x in vector],
            "k": k,
            "num_candidates": self.num_candidates(k),
        }
        if flt is not None:
            knn["filter"] = flt  # inside knn: a sibling filter would be applied after the top-k
        if entry.options.get("index_options", self._index_options) in QUANTIZED and self._supports_rescore():
            knn["rescore_vector"] = {"oversample": RESCORE_OVERSAMPLE}
        return self._client.search(index=index, knn=knn, size=k, source=False).body


# ---- registration ---------------------------------------------------------------------------


class ElasticsearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, description="Elasticsearch URL, e.g. https://localhost:9200")
    cloud_id: str | None = Field(default=None, description="Elastic Cloud ID (use instead of url)")
    username: str | None = Field(default=None, description="User for password authentication (default 'elastic')")
    verify_certs: bool = Field(default=True, description="Verify the server's TLS certificate")
    ca_certs: str | None = Field(default=None, description="Path to a CA bundle for a private certificate authority")
    request_timeout: float = Field(default=30.0, gt=0, description="Seconds before a request times out")
    index_options: IndexOptions = Field(
        default="hnsw",
        description="Vector index type (set once): hnsw is full precision; int8_/int4_/bbq_ variants quantise "
        "for memory at some cost in recall (bbq needs 64+ dimensions); flat variants scan exactly",
    )
    num_candidates_factor: int = Field(
        default=10, ge=1, le=1000, description="num_candidates = k x this factor (capped at 10,000): recall vs speed"
    )
    prefix: str = Field(default="", description="Prefix prepended to the Elasticsearch index name")

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        return check_index_prefix(v)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("url must start with http:// or https://")
        return v

    @model_validator(mode="after")
    def _one_target(self) -> ElasticsearchConfig:
        if (self.url is None) == (self.cloud_id is None):
            raise ValueError("set exactly one of url and cloud_id")
        return self


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> ElasticsearchStore:
    assert isinstance(config, ElasticsearchConfig)
    return ElasticsearchStore.connect(
        url=config.url,
        cloud_id=config.cloud_id,
        api_key=secrets.get("api_key") or None,
        username=config.username,
        password=secrets.get("password") or None,
        verify_certs=config.verify_certs,
        ca_certs=config.ca_certs,
        request_timeout=config.request_timeout,
        prefix=config.prefix,
        num_candidates_factor=config.num_candidates_factor,
        index_options=config.index_options,
    )


STORE_TYPE = VectorStoreType(
    type="elasticsearch",
    label="Elasticsearch",
    description="Elasticsearch 8.11+ or Elastic Cloud (dense_vector with HNSW). Needs the optional 'elasticsearch' extra.",
    config_model=ElasticsearchConfig,
    secret_keys=("api_key", "password"),
    capabilities=CAPABILITIES,
    factory=_factory,
    immutable_fields=("index_options",),
)
