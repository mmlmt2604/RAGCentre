"""Chroma vector store (optional extra ``ragcentre[chroma]``).

The distance space is set when a collection is created (``configuration.hnsw.space``). The
collection's dimension and filterable fields are kept in its metadata (``rc_dim``, ``rc_fields``).
Collections are always opened with ``embedding_function=None``: RAGCentre supplies every vector,
and Chroma must never fall back to its default embedding model.

Scores: Chroma returns distances. cosine → 1 − d; inner product (Chroma's ``ip`` is 1 − dot) →
1 − d; l2 is **squared** Euclidean distance → −√d.

Chroma's ``where`` is nearly the filter language itself. It needs a single top-level key, so
multi-condition filters become an explicit ``$and``; the parsed filter never has a one-operand
``$and``/``$or``. Float fields are written and filtered as floats because Chroma requires every
value in an ``$in``/``$nin`` list to share one type.

``chromadb`` is imported only when a store is created, so ``import ragcentre`` works without the
extra.
"""

from __future__ import annotations

import contextlib
import json
import math
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import And, Cond, FieldType, Filter, Or
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
from ragcentre.stores.registry import VectorStoreType

DEFAULT_MAX_BATCH = 1000
_SPACES: dict[Metric, str] = {"cosine": "cosine", "l2": "l2", "dot": "ip"}


def _import() -> Any:
    return require_extra("chromadb", store="chroma", extra="chroma", package="chromadb")


def _client_settings(chromadb: Any, **extra: Any) -> Any:
    return chromadb.config.Settings(anonymized_telemetry=False, **extra)


def _score(metric: Metric, distance: float) -> float:
    if metric == "l2":
        return -math.sqrt(max(distance, 0.0))
    return 1.0 - distance


def _value(v: Any, ftype: FieldType) -> Any:
    return float(v) if ftype == "float" and not isinstance(v, bool) else v


def translate_filter(f: Filter, fields: dict[str, FieldType]) -> dict[str, Any]:
    if isinstance(f, (And, Or)):
        key = "$and" if isinstance(f, And) else "$or"
        return {key: [translate_filter(item, fields) for item in f.items]}
    assert isinstance(f, Cond)
    ftype = fields.get(f.field, "str")
    if isinstance(f.value, tuple):
        return {f.field: {f.op: [_value(v, ftype) for v in f.value]}}
    return {f.field: {f.op: _value(f.value, ftype)}}


class ChromaStore:
    # The core batches upserts by this; each upsert is split further if the client reports a
    # smaller maximum batch size.
    capabilities: ClassVar[Capabilities] = Capabilities(
        metrics=frozenset({"cosine", "l2", "dot"}), max_batch=DEFAULT_MAX_BATCH
    )

    def __init__(self, client: Any) -> None:
        self._chromadb = _import()
        self._client = client
        try:
            reported = int(client.get_max_batch_size())
        except Exception:
            reported = DEFAULT_MAX_BATCH
        self.client_max_batch = max(1, min(reported, DEFAULT_MAX_BATCH))
        self._lock = threading.RLock()

    @classmethod
    def ephemeral(cls) -> ChromaStore:
        chromadb = _import()
        return cls(chromadb.EphemeralClient(settings=_client_settings(chromadb, allow_reset=True)))

    @classmethod
    def persistent(cls, path: str | Path) -> ChromaStore:
        chromadb = _import()
        return cls(chromadb.PersistentClient(path=str(path), settings=_client_settings(chromadb)))

    @classmethod
    def http(cls, host: str, port: int, ssl: bool = False, headers: dict[str, str] | None = None) -> ChromaStore:
        chromadb = _import()
        client = chromadb.HttpClient(host=host, port=port, ssl=ssl, headers=headers,
                                     settings=_client_settings(chromadb))  # fmt: skip
        return cls(client)

    # ---- helpers ------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _errors(self, name: str, what: str) -> Iterator[None]:
        try:
            yield
        except (StoreError, CollectionMismatch):
            raise
        except self._chromadb.errors.NotFoundError as exc:
            raise CollectionNotFound(f"collection {name} does not exist") from exc
        except Exception as exc:
            raise StoreError(f"chroma: {what} on {name} failed: {exc}") from exc

    def _get(self, name: str) -> Any | None:
        check_collection_name(name)
        with self._errors(name, "get collection"):
            try:
                return self._client.get_collection(name, embedding_function=None)
            except self._chromadb.errors.NotFoundError:
                return None

    def _require(self, name: str) -> tuple[Any, int, Metric, dict[str, FieldType]]:
        col = self._get(name)
        if col is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        dim, metric, fields = self._describe(col)
        return col, dim, metric, fields

    @staticmethod
    def _describe(col: Any) -> tuple[int, Metric, dict[str, FieldType]]:
        meta = col.metadata or {}
        if "rc_dim" not in meta:
            raise StoreError(f"chroma collection {col.name} was not created by RAGCentre")
        space = ((col.configuration or {}).get("hnsw") or {}).get("space") or "l2"
        metric = next(m for m, s in _SPACES.items() if s == space)
        return int(meta["rc_dim"]), metric, json.loads(meta.get("rc_fields") or "{}")

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        try:
            self._client.heartbeat()
        except Exception as exc:
            raise StoreError(f"chroma is not reachable: {exc}") from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        with self._lock, self._errors(spec.name, "ensure collection"):
            col = self._client.get_or_create_collection(
                spec.name,
                configuration={"hnsw": {"space": _SPACES[spec.metric]}},
                metadata={"rc_dim": spec.dim, "rc_fields": json.dumps(dict(spec.fields), sort_keys=True)},
                embedding_function=None,
            )
            dim, metric, fields = self._describe(col)
            if dim != spec.dim or metric != spec.metric:
                raise CollectionMismatch(
                    f"collection {spec.name} exists with dim={dim}, metric={metric}; "
                    f"requested dim={spec.dim}, metric={spec.metric}"
                )
            for fname, ftype in spec.fields.items():
                if fname in fields and fields[fname] != ftype:
                    raise CollectionMismatch(f"field {fname} is {fields[fname]} in {spec.name}, not {ftype}")
            merged = {**fields, **spec.fields}
            if merged != fields:
                col.modify(metadata={**(col.metadata or {}), "rc_fields": json.dumps(merged, sort_keys=True)})

    def collection_info(self, name: str) -> CollectionInfo | None:
        col = self._get(name)
        if col is None:
            return None
        dim, metric, _ = self._describe(col)
        with self._errors(name, "count"):
            count = int(col.count())
        return CollectionInfo(dim=dim, metric=metric, count=count)

    def drop_collection(self, name: str) -> None:
        check_collection_name(name)
        with (
            self._lock,
            self._errors(name, "drop collection"),
            contextlib.suppress(self._chromadb.errors.NotFoundError),
        ):
            self._client.delete_collection(name)

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        if not records:
            return
        col, dim, _, fields = self._require(name)
        for rec in records:
            if len(rec.vector) != dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {dim}")
        for start in range(0, len(records), self.client_max_batch):
            part = records[start : start + self.client_max_batch]
            with self._errors(name, "upsert"):
                col.upsert(
                    ids=[r.id for r in part],
                    embeddings=[list(map(float, r.vector)) for r in part],
                    # Chroma rejects an empty metadata dict; None means "no metadata".
                    metadatas=[
                        {k: _value(v, fields.get(k, "str")) for k, v in r.metadata.items()} or None for r in part
                    ],
                )

    def delete(self, name: str, ids: Sequence[str]) -> None:
        if not ids:
            return
        col = self._get(name)
        if col is None:
            return
        with contextlib.suppress(CollectionNotFound), self._errors(name, "delete"):
            col.delete(ids=list(ids))

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        col, dim, metric, fields = self._require(name)
        if len(vector) != dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {dim}")
        if k < 1:
            return []
        where = None if filter is None else translate_filter(filter, {**fields, "document_id": "str"})
        with self._errors(name, "query"):
            res = col.query(query_embeddings=[list(map(float, vector))], n_results=k, where=where,
                            include=["distances"])  # fmt: skip
        ids = (res.get("ids") or [[]])[0]
        distances = (res.get("distances") or [[]])[0]
        return [Match(id=i, score=_score(metric, float(d))) for i, d in zip(ids, distances, strict=True)]

    def close(self) -> None:
        pass  # Chroma clients hold no resources that need explicit release


# ---- registration ---------------------------------------------------------------------------


class ChromaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str | None = Field(default=None, description="Chroma server host (instead of path)")
    port: int = Field(default=8000, ge=1, le=65535, description="Chroma server port")
    ssl: bool = Field(default=False, description="Use HTTPS for the Chroma server")
    path: str | None = Field(
        default=None, description="Directory for a persistent local client (default <data dir>/chroma)"
    )

    @model_validator(mode="after")
    def _one_location(self) -> ChromaConfig:
        if self.host is not None and self.path is not None:
            raise ValueError("set host or path, not both")
        return self


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> ChromaStore:
    assert isinstance(config, ChromaConfig)
    if config.host is not None:
        token = secrets.get("token")
        headers = {"Authorization": f"Bearer {token}"} if token else None
        return ChromaStore.http(config.host, config.port, config.ssl, headers)
    path = Path(config.path) if config.path else data_dir / "chroma"
    if not path.is_absolute():
        path = data_dir / path
    return ChromaStore.persistent(path)


STORE_TYPE = VectorStoreType(
    type="chroma",
    label="Chroma",
    description="Chroma server, or a persistent local Chroma directory. Needs the optional 'chroma' extra.",
    config_model=ChromaConfig,
    secret_keys=("token",),
    capabilities=ChromaStore.capabilities,
    factory=_factory,
)
