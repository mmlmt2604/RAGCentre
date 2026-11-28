"""Weaviate vector store (optional extra ``ragcentre[weaviate]``, weaviate-client v4).

One RAGCentre collection is one Weaviate collection ("class") with ``vectorizer: none``; RAGCentre
supplies every vector, and Weaviate never calls an embedding module. Facts below were checked
against weaviate-client 4.24.1 and a Weaviate 1.36.2 server (see ``DECISIONS`` in the fragment).

**Names.** Weaviate class names must match ``^[A-Z][_0-9A-Za-z]*$`` and the server upper-cases the
first character itself. The physical name is ``capitalize(prefix + logical)``: ``rc_x`` becomes
``Rc_x`` and, with ``prefix="team_"``, ``Team_rc_x``. Logical names start with a lower-case letter
and a prefix must too, so the mapping is injective for one prefix and reversible
(``logical_name``): lower-case the first character, then strip the prefix. Two stores sharing a
server with *different* prefixes could collide only if ``p1 + n1 == p2 + n2``; pick prefixes that
are not suffixes of each other's names.

**Properties.** Weaviate reserves ``id``, ``_id``, ``_additional`` and ``vector`` (case-insensitive
on the first letter), treats ``Name`` and ``name`` as one property, and rejects names that are not ``[_A-Za-z][_0-9A-Za-z]*``. RAGCentre field names pass
that pattern, but may be reserved or collide (the server compares property names without regard to
case), so every schema field ``x`` is stored as property ``f_x`` (``fu_...`` if it has capitals; see
``property_name``). The record id lives in the object UUID; ids that are not already canonical lower-case
UUIDs (Weaviate canonicalises UUIDs) get a deterministic ``uuid5`` and keep the original id in the
``rc_id`` property, so ids round-trip exactly.

**Strings.** Text properties use ``field`` tokenization (exact match, case-sensitive). Weaviate
still trims leading and trailing whitespace (including U+00A0 and U+3000) from indexed values and
filter values, which would make ``"en "`` equal ``"en"``; the empty string is also awkward. So
string values are stored wrapped in double quotes (``"en"``), and filter values likewise; the
wrapping never reaches RAGCentre.

**Scores** (verified on the server): cosine distance ``d = 1 - cos`` gives ``1 - d`` (a zero vector
has distance 1, so score 0); ``dot`` distance is ``-dot`` gives ``-d``; ``l2-squared`` gives
``-sqrt(d)``.

**Catalog.** The collection's dimension, metric, filter fields and index options are a JSON
``InStoreCatalog`` entry in the collection ``description`` (written atomically at creation, updated
with ``config.update(description=...)``). Adding a field adds the property first, then the
description (read-merge-write; Weaviate has no compare-and-set, so concurrent field additions from
separate processes can still race, and the read-back in ``InStoreCatalog`` turns a lost one into a
``StoreError`` the caller retries). Weaviate does not index a property added later for existing objects, which is fine:
a new schema field rewrites every record anyway.

**Filters** compile to ``Filter.by_property`` with ``all_of`` / ``any_of`` (the ``&`` / ``|``
operators); ``$ne`` is ``not_equal`` (also true for objects without the property), ``$in`` is
``contains_any`` and ``$nin`` is a conjunction of ``not_equal``. Nothing is post-filtered.

``weaviate`` is imported only when a store is created, so ``import ragcentre`` works without the
extra.
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
from ragcentre.stores.catalog import CatalogEntry, InStoreCatalog, reconcile
from ragcentre.stores.filter_sql import check_field
from ragcentre.stores.registry import VectorStoreType

DESC_PREFIX = "ragcentre-catalog:"
FIELD_PREFIX = "f_"  # schema field x -> Weaviate property f_x (avoids reserved names; see property_name)
ID_PROP = "rc_id"  # original record id, written only when it is not a canonical lower-case UUID
MAX_DIM = 65535  # verified: 65535 accepted, 65536 rejected by the server
MAX_K = 10000  # Weaviate's default QUERY_MAXIMUM_RESULTS
MAX_BATCH = 256
WRITE_ATTEMPTS = 8  # rounds of read-merge-write when workers update the catalog at once
_ID_NAMESPACE = uuid.UUID("5d3f0f0e-3b0e-5a52-9b7c-52414743656e")
_CLASS_RE = re.compile(r"^[A-Z][_0-9A-Za-z]{0,254}$")
_PREFIX_RE = re.compile(r"^(?:[a-z][a-z0-9_]{0,40})?$")
_CLOUD_SUFFIXES = (".weaviate.cloud", ".weaviate.network")

_DISTANCES: dict[Metric, str] = {"cosine": "COSINE", "l2": "L2_SQUARED", "dot": "DOT"}
_DATA_TYPES: dict[FieldType, str] = {"str": "TEXT", "int": "INT", "float": "NUMBER", "bool": "BOOL"}


def _import() -> Any:
    return require_extra("weaviate", store="weaviate", extra="weaviate", package="weaviate-client")


# ---- naming, ids, values -------------------------------------------------------------------


def physical_name(prefix: str, name: str) -> str:
    """Map a logical collection name to its Weaviate class name (see the module docstring)."""
    check_collection_name(name)
    joined = prefix + name
    physical = joined[0].upper() + joined[1:]
    if not _CLASS_RE.match(physical):
        raise StoreError(
            f"collection name {physical!r} (prefix {prefix!r} plus {name!r}) is not a valid Weaviate class name"
        )
    return physical


def logical_name(prefix: str, physical: str) -> str | None:
    """The inverse of ``physical_name``; ``None`` if ``physical`` is not one of this prefix's names."""
    if not physical:
        return None
    joined = physical[0].lower() + physical[1:]
    if not joined.startswith(prefix):
        return None
    name = joined[len(prefix) :]
    try:
        return check_collection_name(name)
    except StoreError:
        return None


def property_name(field: str) -> str:
    """The Weaviate property for a schema field.

    Weaviate treats property names case-insensitively (``f_Name`` conflicts with ``f_name``), so the
    result is always lower-case. A field with no upper-case letter is ``f_<field>``. Otherwise it is
    ``fu_`` plus the field with ``_`` doubled and each capital ``X`` written as ``_x``, which keeps
    the mapping injective (a single ``_`` can only introduce a capital).
    """
    check_field(field)
    if field == field.lower():
        return FIELD_PREFIX + field
    out = []
    for ch in field:
        out.append("__" if ch == "_" else "_" + ch.lower() if ch.isupper() else ch)
    return "fu_" + "".join(out)


def object_uuid(record_id: str) -> uuid.UUID:
    """Canonical lower-case UUID ids are used as they are; anything else gets a stable ``uuid5``."""
    try:
        parsed = uuid.UUID(record_id)
    except ValueError:
        return uuid.uuid5(_ID_NAMESPACE, record_id)
    return parsed if str(parsed) == record_id else uuid.uuid5(_ID_NAMESPACE, record_id)


def encode_str(value: str) -> str:
    return f'"{value}"'


def _stored(value: Any, ftype: FieldType) -> Any:
    if ftype == "str":
        return encode_str(str(value))
    if ftype == "float":
        return float(value)
    if ftype == "int":
        return int(value)
    return bool(value)


def score(metric: Metric, distance: float) -> float:
    if metric == "cosine":
        return 1.0 - distance
    if metric == "dot":
        return -distance  # Weaviate reports the negative dot product
    return -math.sqrt(max(distance, 0.0))  # l2-squared


# ---- filter translation --------------------------------------------------------------------


def translate_filter(f: Filter, fields: dict[str, FieldType]) -> Any:
    """Compile a parsed filter to a weaviate ``Filter`` (``all_of`` / ``any_of`` for ``&`` / ``|``)."""
    wf = _import().classes.query.Filter
    return _translate(f, fields, wf)


def _translate(f: Filter, fields: dict[str, FieldType], wf: Any) -> Any:
    if isinstance(f, And):
        return wf.all_of([_translate(i, fields, wf) for i in f.items])
    if isinstance(f, Or):
        return wf.any_of([_translate(i, fields, wf) for i in f.items])
    assert isinstance(f, Cond)
    ftype = "str" if f.field == "document_id" else fields.get(f.field)
    if ftype is None:
        raise StoreError(f"weaviate: unknown filter field {f.field!r}; the collection has: {', '.join(sorted(fields))}")
    prop = wf.by_property(property_name(f.field))
    if f.op in ("$in", "$nin"):
        assert isinstance(f.value, tuple)
        if not f.value:
            raise StoreError(f"weaviate: {f.op} needs at least one value")
        values = [_stored(v, ftype) for v in f.value]
        if f.op == "$in":
            return prop.contains_any(values)
        parts = [wf.by_property(property_name(f.field)).not_equal(v) for v in values]
        return parts[0] if len(parts) == 1 else wf.all_of(parts)
    assert not isinstance(f.value, tuple)
    value = _stored(f.value, ftype)
    if f.op == "$eq":
        return prop.equal(value)
    if f.op == "$ne":
        return prop.not_equal(value)
    if ftype not in ("int", "float"):
        raise StoreError(f"weaviate: {f.op} applies to int and float fields only; {f.field} is {ftype}")
    return {"$gt": prop.greater_than, "$gte": prop.greater_or_equal, "$lt": prop.less_than, "$lte": prop.less_or_equal}[
        f.op
    ](value)


# ---- store ---------------------------------------------------------------------------------


class WeaviateStore:
    capabilities: ClassVar[Capabilities] = Capabilities(
        metrics=frozenset({"cosine", "l2", "dot"}),
        max_batch=MAX_BATCH,
        max_dim=MAX_DIM,
        consistency="immediate",  # single node: writes are visible to the next query (contract run)
        filter_exact=True,  # allow-list pre-filter with flat search for selective filters (contract run)
    )

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = "",
        hnsw_max_connections: int | None = None,
        hnsw_ef_construction: int | None = None,
        hnsw_ef: int | None = None,
    ) -> None:
        if not _PREFIX_RE.match(prefix):
            raise StoreError("weaviate prefix must start with a lower-case letter and use [a-z0-9_] only")
        self._w = _import()
        self._client = client
        self._prefix = prefix
        self._max_connections = hnsw_max_connections
        self._ef_construction = hnsw_ef_construction
        self._ef = hnsw_ef
        self._meta_cache: dict[str, CatalogEntry] = {}
        self._cache_lock = threading.Lock()
        self._catalog_lock = threading.RLock()
        self._catalog = InStoreCatalog(self._read_catalog, self._write_catalog, transaction=lambda: self._catalog_lock)

    @classmethod
    def connect(
        cls,
        *,
        url: str | None = None,
        host: str | None = None,
        port: int = 8080,
        grpc_port: int = 50051,
        secure: bool = False,
        api_key: str | None = None,
        timeout: int | None = None,
        prefix: str = "",
        hnsw_max_connections: int | None = None,
        hnsw_ef_construction: int | None = None,
        hnsw_ef: int | None = None,
    ) -> WeaviateStore:
        w = _import()
        auth = w.classes.init.Auth.api_key(api_key) if api_key else None
        extra = (
            w.classes.init.AdditionalConfig(timeout=w.classes.init.Timeout(init=timeout, query=timeout, insert=timeout))
            if timeout
            else None
        )
        try:
            if url is not None:
                parsed = urlparse(url)
                host = parsed.hostname or ""
                secure = parsed.scheme == "https"
                port = parsed.port or (443 if secure else 80)
                if host.endswith(_CLOUD_SUFFIXES):  # Weaviate Cloud has its own gRPC host; not run in tests
                    client = w.connect_to_weaviate_cloud(
                        cluster_url=url, auth_credentials=auth, additional_config=extra
                    )
                    return cls(client, prefix=prefix, hnsw_max_connections=hnsw_max_connections,
                               hnsw_ef_construction=hnsw_ef_construction, hnsw_ef=hnsw_ef)  # fmt: skip
            assert host
            client = w.connect_to_custom(
                http_host=host, http_port=port, http_secure=secure,
                grpc_host=host, grpc_port=grpc_port, grpc_secure=secure,
                auth_credentials=auth, additional_config=extra,
            )  # fmt: skip
        except Exception as exc:
            raise StoreError(f"weaviate is not reachable at {host}:{port} (gRPC {grpc_port}): {exc}") from exc
        return cls(client, prefix=prefix, hnsw_max_connections=hnsw_max_connections,
                   hnsw_ef_construction=hnsw_ef_construction, hnsw_ef=hnsw_ef)  # fmt: skip

    # ---- helpers ------------------------------------------------------------------------------

    def _physical(self, name: str) -> str:
        return physical_name(self._prefix, name)

    def _options(self) -> dict[str, Any]:
        return {"hnsw_max_connections": self._max_connections, "hnsw_ef_construction": self._ef_construction}

    def _exists(self, physical: str) -> bool:
        try:
            return bool(self._client.collections.exists(physical))
        except Exception as exc:
            raise StoreError(f"weaviate: could not look up collection {physical}: {exc}") from exc

    def _config(self, physical: str) -> Any | None:
        try:
            return self._client.collections.get(physical).config.get()
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise StoreError(f"weaviate: could not read collection {physical}: {exc}") from exc

    def _read_catalog(self, name: str) -> str | None:
        cfg = self._config(self._physical(name))
        if cfg is None:
            return None
        description = cfg.description or ""
        if not description.startswith(DESC_PREFIX):
            raise StoreError(f"weaviate collection {self._physical(name)} was not created by RAGCentre")
        return str(description[len(DESC_PREFIX) :])

    def _write_catalog(self, name: str, text: str) -> None:
        """Add the properties for any new fields, then record the entry in the description.

        Weaviate has no compare-and-set on the description, and ``config.update`` rewrites the whole
        class, so another worker adding a field at the same moment can make this write fail or
        overwrite it. The loop re-reads, merges the other worker's fields, writes, and checks that
        every field survived; it gives up after a few rounds.
        """
        physical = self._physical(name)
        wanted = CatalogEntry.from_json(text)
        col = self._client.collections.get(physical)
        last: Exception | None = None
        for attempt in range(WRITE_ATTEMPTS):
            if attempt:
                time.sleep(random.uniform(0.02, 0.1) * attempt)
            cfg = self._config(physical)
            if cfg is None:
                raise CollectionNotFound(f"collection {name} does not exist")
            description = cfg.description or ""
            entry = wanted
            if description.startswith(DESC_PREFIX):
                theirs = CatalogEntry.from_json(description[len(DESC_PREFIX) :])
                entry = replace(wanted, fields={**theirs.fields, **wanted.fields})
            have = {p.name for p in cfg.properties}
            try:
                for fname, ftype in entry.fields.items():
                    if property_name(fname) not in have:
                        try:
                            col.config.add_property(self._property(fname, ftype))
                        except Exception as exc:
                            if "already exists" not in str(exc):  # a concurrent worker added it
                                raise
                col.config.update(description=DESC_PREFIX + entry.to_json())
            except Exception as exc:
                last = exc  # most often: the class changed between our read and our write
                continue
            after = self._config(physical)
            stored = (after.description or "") if after is not None else ""
            if stored.startswith(DESC_PREFIX) and wanted.fields.items() <= (
                CatalogEntry.from_json(stored[len(DESC_PREFIX) :]).fields.items()
            ):
                return
        raise StoreError(f"weaviate: could not update collection {name}: {last or 'concurrent catalog updates'}")

    def _property(self, fname: str, ftype: FieldType) -> Any:
        cfg = self._w.classes.config
        data_type = getattr(cfg.DataType, _DATA_TYPES[ftype])
        if ftype == "str":
            return cfg.Property(
                name=property_name(fname), data_type=data_type, tokenization=cfg.Tokenization.FIELD,
                index_filterable=True, index_searchable=False,
            )  # fmt: skip
        if ftype in ("int", "float"):
            return cfg.Property(name=property_name(fname), data_type=data_type, index_range_filters=True)
        return cfg.Property(name=property_name(fname), data_type=data_type)

    def _create(self, spec: CollectionSpec, entry: CatalogEntry) -> None:
        cfg = self._w.classes.config
        index = cfg.Configure.VectorIndex.hnsw(
            distance_metric=getattr(cfg.VectorDistances, _DISTANCES[spec.metric]),
            max_connections=self._max_connections,
            ef_construction=self._ef_construction,
            ef=self._ef,
        )
        properties = [self._property(f, t) for f, t in entry.fields.items()]
        properties.append(
            cfg.Property(name=ID_PROP, data_type=cfg.DataType.TEXT, index_filterable=False, index_searchable=False)
        )
        self._client.collections.create(
            self._physical(spec.name),
            description=DESC_PREFIX + entry.to_json(),
            vector_config=cfg.Configure.Vectors.self_provided(vector_index_config=index),
            properties=properties,
        )

    def _entry(self, name: str) -> CatalogEntry:
        check_collection_name(name)
        with self._cache_lock:
            cached = self._meta_cache.get(name)
        if cached is not None:
            return cached
        text = self._read_catalog(name)
        if text is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        entry = CatalogEntry.from_json(text)
        with self._cache_lock:
            self._meta_cache[name] = entry
        return entry

    def _forget(self, name: str) -> None:
        with self._cache_lock:
            self._meta_cache.pop(name, None)

    def _missing(self, name: str, exc: Exception) -> bool:
        """Is this error "the collection is gone"? Forgets the cached catalog if so."""
        text = str(exc).lower()
        gone = "could not find class" in text or getattr(exc, "status_code", None) == 404
        if gone or not self._exists(self._physical(name)):
            self._forget(name)
            return True
        return False

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        try:
            if not self._client.is_live():
                raise StoreError("weaviate is not live")
            if not self._client.is_ready():
                raise StoreError("weaviate is not ready")
            self._client.collections.list_all(simple=True)  # needs read access to the schema
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"weaviate is not reachable: {exc}") from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        if spec.fields.get("document_id", "str") != "str":
            raise StoreError("field document_id must be a str field")
        spec = replace(spec, fields={"document_id": "str", **spec.fields})
        physical = self._physical(spec.name)
        options = self._options()
        self._forget(spec.name)
        if not self._exists(physical):
            try:
                self._create(spec, reconcile(None, spec, options))
                return
            except Exception as exc:
                if not self._exists(physical):  # not a concurrent creation
                    raise StoreError(f"weaviate: could not create collection {spec.name}: {exc}") from exc
        # The collection exists (or was just created by another worker): check it, add new fields.
        self._catalog.ensure(spec, options)
        if self._ef is not None:
            self._apply_ef(physical)

    def _apply_ef(self, physical: str) -> None:
        with self._catalog_lock:
            cfg = self._config(physical)
            if cfg is None:
                return
            current = cfg.vector_config["default"].vector_index_config
            if getattr(current, "ef", None) == self._ef:
                return
            rc = self._w.classes.config.Reconfigure
            try:
                self._client.collections.get(physical).config.update(
                    vector_config=rc.Vectors.update(
                        name="default", vector_index_config=rc.VectorIndex.hnsw(ef=self._ef)
                    )
                )
            except Exception as exc:
                raise StoreError(f"weaviate: could not set hnsw ef on {physical}: {exc}") from exc

    def collection_info(self, name: str) -> CollectionInfo | None:
        check_collection_name(name)
        physical = self._physical(name)
        cfg = self._config(physical)
        if cfg is None:
            return None
        text = self._read_catalog(name)
        assert text is not None
        entry = CatalogEntry.from_json(text)
        index = cfg.vector_config["default"].vector_index_config
        metric = next((m for m, d in _DISTANCES.items() if d == index.distance_metric.name), entry.metric)
        try:
            count = self._client.collections.get(physical).aggregate.over_all(total_count=True).total_count
        except Exception as exc:
            if not self._exists(physical):
                return None
            raise StoreError(f"weaviate: could not count {name}: {exc}") from exc
        return CollectionInfo(dim=entry.dim, metric=metric, count=int(count or 0))

    def drop_collection(self, name: str) -> None:
        check_collection_name(name)
        self._forget(name)
        try:
            self._client.collections.delete(self._physical(name))  # a missing collection is not an error
        except Exception as exc:
            if not self._exists(self._physical(name)):
                return
            raise StoreError(f"weaviate: could not drop collection {name}: {exc}") from exc

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        entry = self._entry(name)
        fields = {"document_id": "str", **entry.fields}
        refreshed = False
        objects = []
        for rec in records:
            if len(rec.vector) != entry.dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {entry.dim}")
            props: dict[str, Any] = {}
            for key, value in rec.metadata.items():
                if key not in fields and not refreshed:
                    # Another worker may have added the field since this catalog was cached.
                    refreshed = True
                    self._forget(name)
                    fields = {"document_id": "str", **self._entry(name).fields}
                if key in fields:  # undeclared metadata is not filterable, so it is not stored
                    props[property_name(key)] = _stored(value, fields[key])
            oid = object_uuid(rec.id)
            if str(oid) != rec.id:
                props[ID_PROP] = rec.id
            objects.append(self._w.classes.data.DataObject(properties=props, vector=list(rec.vector), uuid=oid))
        if not objects:
            return
        try:
            result = self._client.collections.get(self._physical(name)).data.insert_many(objects)
        except Exception as exc:
            if self._missing(name, exc):
                raise CollectionNotFound(f"collection {name} does not exist") from exc
            raise StoreError(f"weaviate: upsert on {name} failed: {exc}") from exc
        if result.has_errors:
            first = next(iter(result.errors.values()))
            raise StoreError(f"weaviate: upsert on {name} failed for {len(result.errors)} record(s): {first.message}")

    def delete(self, name: str, ids: Sequence[str]) -> None:
        check_collection_name(name)
        if not ids or not self._exists(self._physical(name)):
            return
        where = self._w.classes.query.Filter.by_id().contains_any([object_uuid(i) for i in ids])
        try:
            self._client.collections.get(self._physical(name)).data.delete_many(where=where)
        except Exception as exc:
            if self._missing(name, exc):
                return
            raise StoreError(f"weaviate: delete on {name} failed: {exc}") from exc

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        entry = self._entry(name)
        if len(vector) != entry.dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {entry.dim}")
        if k < 1:
            return []
        if k > MAX_K:
            raise StoreError(
                f"weaviate returns at most {MAX_K} results per query (QUERY_MAXIMUM_RESULTS); asked for {k}"
            )
        flt = None if filter is None else translate_filter(filter, entry.fields)
        try:
            if self._ef is not None:
                self._apply_ef(self._physical(name))
            result = self._client.collections.get(self._physical(name)).query.near_vector(
                near_vector=list(vector),
                limit=k,
                filters=flt,
                return_metadata=self._w.classes.query.MetadataQuery(distance=True),
                return_properties=[ID_PROP],
            )
        except Exception as exc:
            if self._missing(name, exc):
                raise CollectionNotFound(f"collection {name} does not exist") from exc
            raise StoreError(f"weaviate: query on {name} failed: {exc}") from exc
        matches: list[Match] = []
        for obj in result.objects:
            if obj.metadata.distance is None:
                raise StoreError(f"weaviate: query on {name} returned no distance")
            rid = (obj.properties or {}).get(ID_PROP) or str(obj.uuid)
            matches.append(Match(id=str(rid), score=score(entry.metric, float(obj.metadata.distance))))
        matches.sort(key=lambda m: -m.score)
        return matches

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # closing twice or a dead connection is not worth reporting
            return


# ---- registration --------------------------------------------------------------------------


class WeaviateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(
        default=None, description="Weaviate HTTP URL, e.g. http://localhost:8080 (instead of host/port)"
    )
    host: str | None = Field(default=None, description="Weaviate host name (instead of url)")
    port: int | None = Field(default=None, ge=1, le=65535, description="HTTP port with host (default 8080)")
    grpc_port: int = Field(default=50051, ge=1, le=65535, description="gRPC port (Weaviate needs HTTP and gRPC)")
    secure: bool = Field(default=False, description="Use TLS with host (a url's scheme decides otherwise)")
    timeout: int | None = Field(default=None, ge=1, description="Connect, query and insert timeout in seconds")
    prefix: str = Field(default="", description="Prefix for the Weaviate class name (lower-case letters, digits, '_')")
    hnsw_max_connections: int | None = Field(default=None, ge=4, description="HNSW maxConnections (set once)")
    hnsw_ef_construction: int | None = Field(default=None, ge=4, description="HNSW efConstruction (set once)")
    hnsw_ef: int | None = Field(default=None, ge=-1, description="HNSW search-time ef (-1 = dynamic; query time)")

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        if not _PREFIX_RE.match(v):
            raise ValueError("prefix must start with a lower-case letter and contain only a-z, 0-9 and '_'")
        return v

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        if v is not None:
            parsed = urlparse(v)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("url must look like http://host:8080 or https://host")
        return v

    @model_validator(mode="after")
    def _one_location(self) -> WeaviateConfig:
        if (self.url is None) == (self.host is None):
            raise ValueError("set exactly one of url or host")
        if self.url is not None and (self.port is not None or self.secure):
            raise ValueError("port and secure go with host; a url carries its own port and scheme")
        return self


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> WeaviateStore:
    assert isinstance(config, WeaviateConfig)
    return WeaviateStore.connect(
        url=config.url,
        host=config.host,
        port=config.port or 8080,
        grpc_port=config.grpc_port,
        secure=config.secure,
        api_key=secrets.get("api_key") or None,
        timeout=config.timeout,
        prefix=config.prefix,
        hnsw_max_connections=config.hnsw_max_connections,
        hnsw_ef_construction=config.hnsw_ef_construction,
        hnsw_ef=config.hnsw_ef,
    )


STORE_TYPE = VectorStoreType(
    type="weaviate",
    label="Weaviate",
    description="Weaviate server (HTTP + gRPC), vectors supplied by RAGCentre. Needs the optional 'weaviate' extra.",
    config_model=WeaviateConfig,
    secret_keys=("api_key",),
    capabilities=WeaviateStore.capabilities,
    factory=_factory,
    immutable_fields=("hnsw_max_connections", "hnsw_ef_construction"),
)
