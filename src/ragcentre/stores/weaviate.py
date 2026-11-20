"""Weaviate with caller-provided vectors and typed, exactly tokenized metadata.

Collection descriptions retain the immutable shape; native property definitions retain field
types. Additive schema updates therefore do not overwrite another worker's field additions.
No optional client is imported until the provider is opened.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import And, FieldType, Filter, Or
from ragcentre.stores.base import (
    Capabilities,
    CollectionInfo,
    CollectionNotFound,
    CollectionSpec,
    Match,
    Metric,
    Record,
    StoreError,
    check_collection_name,
    check_spec,
    require_extra,
)
from ragcentre.stores.catalog import CatalogEntry, InStoreCatalog, reconcile
from ragcentre.stores.registry import VectorStoreType

_METRICS: dict[Metric, str] = {"cosine": "cosine", "l2": "l2-squared", "dot": "dot"}
_TYPES: dict[FieldType, str] = {"str": "text", "int": "int", "float": "number", "bool": "boolean"}
_MARKER = "ragcentre:"


def _import() -> Any:
    return require_extra("weaviate", store="weaviate", extra="weaviate", package="weaviate-client")


def translate_filter(f: Filter) -> Any:
    WFilter = _import().classes.query.Filter

    if isinstance(f, And):
        return WFilter.all_of([translate_filter(x) for x in f.items])
    if isinstance(f, Or):
        return WFilter.any_of([translate_filter(x) for x in f.items])
    prop = WFilter.by_property("m_" + f.field)
    if f.op in ("$in", "$nin"):
        assert isinstance(f.value, tuple)
        conditions = [prop.equal(v) if f.op == "$in" else prop.not_equal(v) for v in f.value]
        return WFilter.any_of(conditions) if f.op == "$in" else WFilter.all_of(conditions)
    method = {
        "$eq": "equal",
        "$ne": "not_equal",
        "$gt": "greater_than",
        "$gte": "greater_or_equal",
        "$lt": "less_than",
        "$lte": "less_or_equal",
    }[f.op]
    return getattr(prop, method)(f.value)


class WeaviateStore:
    def __init__(self, client: Any, *, collection_prefix: str = "Ragcentre", index_type: str = "flat") -> None:
        self._client = client
        self._prefix = collection_prefix
        self._index_type = index_type
        self._lock = threading.RLock()
        self._catalog = InStoreCatalog(self._read, self._write, transaction=lambda: self._lock)
        self.capabilities = Capabilities(metrics=frozenset(_METRICS), max_batch=128, filter_exact=index_type == "flat")

    @classmethod
    def connect(
        cls,
        *,
        url: str,
        grpc_host: str | None = None,
        grpc_port: int = 50051,
        grpc_secure: bool | None = None,
        api_key: str | None = None,
        collection_prefix: str = "Ragcentre",
        index_type: str = "flat",
    ) -> WeaviateStore:
        w = _import()
        parsed = urlsplit(url)
        secure = parsed.scheme == "https"
        try:
            client = w.connect_to_custom(
                http_host=parsed.hostname,
                http_port=parsed.port or (443 if secure else 80),
                http_secure=secure,
                grpc_host=grpc_host or parsed.hostname,
                grpc_port=grpc_port,
                grpc_secure=secure if grpc_secure is None else grpc_secure,
                auth_credentials=w.auth.Auth.api_key(api_key) if api_key else None,
            )
        except Exception as exc:
            raise StoreError("weaviate: could not connect to the configured HTTP/gRPC endpoints") from exc
        return cls(client, collection_prefix=collection_prefix, index_type=index_type)

    def _physical(self, name: str) -> str:
        check_collection_name(name)
        return self._prefix + "_" + name

    def _collection(self, name: str) -> Any:
        return self._client.collections.use(self._physical(name))

    def _read(self, name: str) -> str | None:
        try:
            if not self._client.collections.exists(self._physical(name)):
                return None
            config = self._collection(name).config.get()
            description = config.description or ""
            if not description.startswith(_MARKER):
                raise StoreError(f"weaviate collection {name} has no RAGCentre catalog")
            entry = CatalogEntry.from_json(description[len(_MARKER) :])
            fields = {}
            for prop in config.properties:
                if prop.name.startswith("m_"):
                    kind = next((k for k, v in _TYPES.items() if v == prop.data_type.value), None)
                    if kind is None:
                        raise StoreError(f"weaviate: unsupported property type for {prop.name}")
                    fields[prop.name[2:]] = kind
            # Physical vector configuration is also checked, so an externally altered metric is
            # never silently accepted. Dimension is enforced by the adapter's persistent catalog.
            vector_config = config.vector_config["default"].vector_index_config
            if vector_config.distance_metric.value != _METRICS[entry.metric]:
                raise CollectionMismatch(f"weaviate collection {name} has a different physical distance metric")
            return CatalogEntry(entry.dim, entry.metric, fields, entry.options).to_json()
        except (StoreError, CollectionMismatch):
            raise
        except Exception as exc:
            raise StoreError(f"weaviate: could not read collection {name}") from exc

    @staticmethod
    def _property(name: str, kind: FieldType) -> Any:
        config = _import().classes.config
        kwargs = {"tokenization": config.Tokenization.FIELD, "index_searchable": False} if kind == "str" else {}
        return config.Property(
            name="m_" + name, data_type=config.DataType(_TYPES[kind]), index_filterable=True, **kwargs
        )

    def _write(self, name: str, text: str) -> None:
        wanted = CatalogEntry.from_json(text)
        current = self._catalog.get(name)
        if current is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        for field, kind in wanted.fields.items():
            if field not in current.fields:
                try:
                    self._collection(name).config.add_property(self._property(field, kind))
                except Exception:
                    # Another worker may have added it. Re-read the native schema and check type.
                    actual = self._catalog.get(name)
                    if actual is None or actual.fields.get(field) != kind:
                        raise

    def _entry(self, name: str) -> CatalogEntry:
        entry = self._catalog.get(name)
        if entry is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        return entry

    def healthcheck(self) -> None:
        try:
            if not self._client.is_ready():
                raise StoreError("weaviate is not ready")
        except Exception as exc:
            raise StoreError("weaviate is not reachable") from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        options = {"index_type": self._index_type}
        with self._lock:
            current = self._catalog.get(spec.name)
            entry = reconcile(current, spec, options)
            if current is None:
                config = _import().classes.config
                index = getattr(config.Configure.VectorIndex, self._index_type)(
                    distance_metric=config.VectorDistances(_METRICS[spec.metric])
                )
                try:
                    self._client.collections.create(
                        name=self._physical(spec.name),
                        description=_MARKER + entry.to_json(),
                        vector_config=config.Configure.Vectors.self_provided(vector_index_config=index),
                        properties=[self._property(k, v) for k, v in {"document_id": "str", **spec.fields}.items()],
                    )
                except Exception as exc:
                    if not self._client.collections.exists(self._physical(spec.name)):
                        raise StoreError(f"weaviate: could not create collection {spec.name}") from exc
            try:
                self._catalog.ensure(spec, options)
            except (CollectionMismatch, StoreError):
                raise
            except Exception as exc:
                raise StoreError(f"weaviate: could not extend collection {spec.name}") from exc

    def collection_info(self, name: str) -> CollectionInfo | None:
        entry = self._catalog.get(name)
        if entry is None:
            return None
        try:
            count = self._collection(name).aggregate.over_all(total_count=True).total_count
            return CollectionInfo(entry.dim, entry.metric, int(count or 0))
        except Exception as exc:
            raise StoreError(f"weaviate: could not count collection {name}") from exc

    def drop_collection(self, name: str) -> None:
        try:
            self._client.collections.delete(self._physical(name))
        except Exception as exc:
            raise StoreError(f"weaviate: could not drop collection {name}") from exc

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch exceeds max_batch {self.capabilities.max_batch}")
        entry = self._entry(name)
        for rec in records:
            if len(rec.vector) != entry.dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; expected {entry.dim}")
        # The batch objects API creates missing UUIDs and replaces existing objects completely.
        # A PUT through data.replace only accepts existing objects on the supported server.
        DataObject = _import().classes.data.DataObject

        if not records:
            return
        try:
            result = self._collection(name).data.insert_many(
                [
                    DataObject(
                        uuid=rec.id, vector=list(rec.vector), properties={"m_" + k: v for k, v in rec.metadata.items()}
                    )
                    for rec in records
                ]
            )
            if result.has_errors:
                raise StoreError(f"weaviate: batch upsert on {name} failed: {result.errors}")
        except Exception as exc:
            raise StoreError(f"weaviate: upsert on {name} failed") from exc

    def delete(self, name: str, ids: Sequence[str]) -> None:
        if not ids or self._catalog.get(name) is None:
            return
        try:
            for record_id in ids:
                self._collection(name).data.delete_by_id(record_id)
        except Exception as exc:
            raise StoreError(f"weaviate: delete on {name} failed") from exc

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        entry = self._entry(name)
        if len(vector) != entry.dim:
            raise StoreError(f"query dimension {len(vector)} differs from {entry.dim}")
        if k < 1:
            return []
        try:
            result = self._collection(name).query.near_vector(
                list(vector),
                limit=k,
                filters=None if filter is None else translate_filter(filter),
                return_metadata=["distance"],
                return_properties=False,
            )
            matches = []
            for obj in result.objects:
                distance = float(obj.metadata.distance)
                score = (
                    1.0 - distance
                    if entry.metric == "cosine"
                    else -math.sqrt(max(distance, 0.0))
                    if entry.metric == "l2"
                    else -distance
                )
                matches.append(Match(str(obj.uuid), score))
            return matches
        except Exception as exc:
            raise StoreError(f"weaviate: query on {name} failed") from exc

    def close(self) -> None:
        self._client.close()


class WeaviateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(default="http://localhost:8080", description="Weaviate HTTP endpoint; cloud uses HTTPS")
    grpc_host: str | None = Field(default=None, description="gRPC hostname; defaults to the HTTP hostname")
    grpc_port: int = Field(default=50051, ge=1, le=65535)
    grpc_secure: bool | None = Field(default=None, description="gRPC TLS; defaults to the HTTP URL's TLS setting")
    collection_prefix: str = Field(default="Ragcentre", pattern=r"^[A-Z][A-Za-z0-9_]{0,31}$")
    index_type: Literal["flat", "hnsw"] = Field(default="flat", description="flat is exact; hnsw is approximate")

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("use an HTTP(S) origin without credentials, path, query or fragment")
        return value


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> WeaviateStore:
    assert isinstance(config, WeaviateConfig)
    return WeaviateStore.connect(**config.model_dump(), api_key=secrets.get("api_key"))


STORE_TYPE = VectorStoreType(
    type="weaviate",
    label="Weaviate",
    description="Weaviate server or cloud with supplied vectors. Needs the 'weaviate' extra.",
    config_model=WeaviateConfig,
    secret_keys=("api_key",),
    capabilities=Capabilities(metrics=frozenset(_METRICS), max_batch=128),
    factory=_factory,
    immutable_fields=("index_type",),
)
