"""Redis vector store (optional extra ``ragcentre[redis]``): Redis 8, or Redis Stack with the search module.

Layout. One search index per collection, over HASH keys ``<prefix><collection>:<id>``. Each hash holds
the vector as a little-endian FLOAT32 blob under ``__rc_vector`` plus one hash field per filterable
metadata field. Fields are indexed as TAG (``str`` and ``bool``; booleans are the tags ``true`` and
``false``) or NUMERIC (``int`` and ``float``). ``document_id`` is always an indexed TAG.

Catalog. The dimension, metric, filter-field types and the index options a collection was built with
are kept in a plain string key ``ragcentre:collection:<prefix><collection>`` holding the catalog JSON
(``InStoreCatalog``). A search index cannot hold this itself: it cannot tell ``int`` from ``float`` or
``str`` from ``bool``. ``collection_info`` reports the dimension and metric of the *index*, which is
what queries run against. ``ensure_collection`` runs under a Redis lock (``ragcentre:lock:<name>``), so
concurrent workers in any number of processes get one creator and the others are checked against it.

Query. ``(<filter>)=>[KNN k @__rc_vector $B AS __rc_score]`` with ``LIMIT 0 k`` and ``DIALECT 2``. The
filter is the KNN pre-filter, so nothing is filtered after the search. The server's hybrid policy is left
on its default, which picks ``ADHOC_BF`` (exact) or ``BATCHES`` (continues until k matches are found or
the index ends). A query that hits the server's ``search-timeout`` (500 ms by default, and the server's
default policy is to return what it has) is reported as an error, never as a short result.

Scores. Verified against Redis 8.10.3 (see DECISIONS): COSINE returns ``1 - cos``, IP returns
``1 - dot`` and L2 returns the *squared* Euclidean distance, so the scores are ``1 - s``, ``1 - s`` and
``-sqrt(s)``.

String values are encoded, not just escaped (``encode_tag``). Verified on the server: a TAG value is
trimmed of leading and trailing whitespace, split on the separator, truncated at a NUL byte, folded to
lower case unless ``CASESENSITIVE``, rejected when empty (without ``INDEXEMPTY``) and dropped from
matching beyond a few thousand characters. Rather than depend on escaping rules, every string becomes
``s`` plus its UTF-8 bytes with everything but ASCII letters and digits written as ``_xx`` (long values:
``h`` plus a SHA-256), so a tag contains only ``[A-Za-z0-9_]`` and equality is exact.

Integers are stored as the server's doubles: integers beyond 2**53 lose precision in filters.

This module is named ``redis`` inside the ``ragcentre.stores`` package. The package uses absolute
imports, so ``import redis`` here is the third-party client; it is imported lazily in ``connect``.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import struct
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ragcentre.errors import CollectionMismatch
from ragcentre.models.filters import And, Cond, FieldType, Filter, Or, type_ok
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
from ragcentre.stores.catalog import CatalogEntry, InStoreCatalog
from ragcentre.stores.filter_sql import FIELD_RE
from ragcentre.stores.registry import VectorStoreType

if TYPE_CHECKING:
    from redis import Redis

VECTOR_FIELD = "__rc_vector"
SCORE_FIELD = "__rc_score"
RESERVED_FIELD_PREFIX = "__rc_"
CATALOG_KEY_PREFIX = "ragcentre:collection:"
LOCK_KEY_PREFIX = "ragcentre:lock:"
TAG_ENCODING_VERSION = 1  # recorded in the catalog; a different encoding would be a different index
DIALECT = 2
DEFAULT_HNSW_EF_RUNTIME = 100  # the server's own default of 10 gives poor recall
INDEX_WAIT_SECONDS = 30.0
LOCK_SECONDS = 120
_MAX_PLAIN_TAG = 512  # encoded characters; longer values are hashed (the server stops matching long tags)
_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
_METRICS: dict[Metric, str] = {"cosine": "COSINE", "l2": "L2", "dot": "IP"}
_METRIC_FROM_SERVER: dict[str, Metric] = {v: k for k, v in _METRICS.items()}
_PREFIX_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-")
_SCHEMES = ("redis://", "rediss://", "unix://")


def _import() -> Any:
    return require_extra("redis", store="redis", extra="redis", package="redis")


# ---- value encoding and the filter compiler -------------------------------------------------


def encode_tag(value: str) -> str:
    """The TAG form of a string value: only ``[A-Za-z0-9_]``, never empty, and one-to-one.

    ``s`` + each UTF-8 byte (ASCII letters and digits as they are, every other byte, including ``_``,
    as ``_`` and two lower-case hex digits). Values whose encoding passes ``_MAX_PLAIN_TAG`` characters
    become ``h`` + the SHA-256 of the value (equality only, which is all a string field supports).
    """
    raw = value.encode("utf-8", "surrogatepass")
    text = "".join(chr(b) if chr(b) in _SAFE else f"_{b:02x}" for b in raw)
    if len(text) > _MAX_PLAIN_TAG:
        return "h" + hashlib.sha256(raw).hexdigest()
    return "s" + text


def _tag_value(value: str | int | float | bool, ftype: FieldType) -> str:
    if ftype == "bool":
        return "true" if value is True else "false"
    if not isinstance(value, str):
        raise StoreError(f"expected a string value for a str field, got {type(value).__name__}")
    return encode_tag(value)


def _number(value: str | int | float | bool) -> str:
    if isinstance(value, (bool, str)):
        raise StoreError(f"expected a number, got {type(value).__name__}")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise StoreError("filter values must be finite numbers")
    return repr(float(value))


def _cond(c: Cond, ftype: FieldType) -> str:
    field = c.field
    if ftype in ("str", "bool"):
        if c.op in ("$eq", "$ne"):
            assert not isinstance(c.value, tuple)
            expr = f"@{field}:{{{_tag_value(c.value, ftype)}}}"
            return expr if c.op == "$eq" else "-" + expr
        if c.op in ("$in", "$nin"):
            assert isinstance(c.value, tuple)
            tags = "|".join(_tag_value(v, ftype) for v in c.value)
            expr = f"@{field}:{{{tags}}}"
            return expr if c.op == "$in" else "-" + expr
        raise StoreError(f"operator {c.op} does not apply to {ftype} field {field}")
    if c.op in ("$gt", "$gte", "$lt", "$lte"):
        assert not isinstance(c.value, tuple)
        n = _number(c.value)
        return {
            "$gt": "@%s:[(%s +inf]",
            "$gte": "@%s:[%s +inf]",
            "$lt": "@%s:[-inf (%s]",
            "$lte": "@%s:[-inf %s]",
        }[c.op] % (field, n)
    if c.op in ("$eq", "$ne"):
        assert not isinstance(c.value, tuple)
        n = _number(c.value)
        expr = f"@{field}:[{n} {n}]"
        return expr if c.op == "$eq" else "-" + expr
    assert isinstance(c.value, tuple)
    points = [f"@{field}:[{n} {n}]" for n in (_number(v) for v in c.value)]
    if c.op == "$in":
        return "|".join(f"({p})" for p in points)
    return " ".join(f"(-{p})" for p in points)  # $nin: not equal to any of them


def compile_filter(f: Filter, fields: Mapping[str, FieldType]) -> str:
    """Compile a parsed filter to a RediSearch query expression (for use inside parentheses).

    ``$eq``/``$in`` on a string or bool are TAG matches on the encoded value; ``$ne``/``$nin`` are the
    negation (a record without the field matches them, as in ``matches``). Numbers use closed or open
    NUMERIC ranges (equality is a point range). ``$and`` joins parenthesised parts with a space, ``$or``
    with ``|``. Every value is encoded (tags) or formatted from a number, so none can add syntax.
    A field the collection does not have raises ``StoreError``.
    """
    if isinstance(f, (And, Or)):
        joiner = " " if isinstance(f, And) else "|"
        return joiner.join(f"({compile_filter(item, fields)})" for item in f.items)
    ftype = "str" if f.field == "document_id" else fields.get(f.field)
    if ftype is None:
        raise StoreError(f"field {f.field!r} is not a filterable field of this collection")
    return _cond(f, ftype)


def to_score(metric: Metric, raw: float) -> float:
    """The server's KNN value to the plan's score (higher is better)."""
    if metric == "l2":
        return -math.sqrt(max(raw, 0.0))  # the server reports the squared distance
    return 1.0 - raw  # cosine: 1 - cos; dot: 1 - ip


# ---- response parsing ---------------------------------------------------------------------


def _s(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _text_keys(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StoreError("redis: unexpected reply from the search module (is protocol RESP3 in use?)")
    return {_s(k): v for k, v in raw.items()}


def parse_matches(reply: Any, key_prefix: str, metric: Metric) -> list[Match]:
    """Matches from an ``FT.SEARCH`` RESP3 reply; raises on a warning (a timeout means missing results)."""
    body = _text_keys(reply)
    warnings = [_s(w) for w in body.get("warning") or []]
    if warnings:
        raise StoreError(f"redis: search returned a warning, so the results may be incomplete: {'; '.join(warnings)}")
    matches: list[Match] = []
    for row in body.get("results") or []:
        item = _text_keys(row)
        key = _s(item["id"])
        if not key.startswith(key_prefix):
            raise StoreError(f"redis: unexpected key {key!r} in results (expected prefix {key_prefix!r})")
        attrs = _text_keys(item.get("extra_attributes") or {})
        matches.append(Match(id=key[len(key_prefix) :], score=to_score(metric, float(_s(attrs[SCORE_FIELD])))))
    matches.sort(key=lambda m: -m.score)  # the server orders by distance; keep ties and rounding stable
    return matches


def _is_auth_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return type(exc).__name__ in ("AuthenticationError", "AuthenticationWrongNumberOfArgsError") or any(
        marker in text
        for marker in ("noauth", "hello must be called", "authentication required", "invalid username-password")
    )


_AUTH_HINT = " (check the password: set the 'password' secret, or credentials in the url)"


def _is_missing_index(exc: Exception) -> bool:
    text = str(exc).lower()
    return "index not found" in text or "unknown index" in text or "no such index" in text


# ---- store ----------------------------------------------------------------------------------


class RedisStore:
    capabilities: ClassVar[Capabilities] = Capabilities(
        metrics=frozenset({"cosine", "l2", "dot"}),
        max_batch=256,
        # Verified: a HASH write is indexed before the command returns, so reads see it at once.
        consistency="immediate",
        filter_exact=True,
        # No limit reported by the server up to 1,000,000 dimensions (FLAT and HNSW).
        max_dim=None,
    )

    def __init__(
        self,
        client: Redis,
        *,
        algorithm: Literal["flat", "hnsw"] = "flat",
        ef_runtime: int | None = None,
        prefix: str = "",
    ) -> None:
        self._rx = _import().exceptions
        self._client: Any = client  # redis-py's execute_command is untyped
        self._algorithm = algorithm
        self._ef_runtime = ef_runtime
        self._prefix = prefix
        self._catalog = InStoreCatalog(self._catalog_read, self._catalog_write, self._catalog_remove)

    @classmethod
    def connect(
        cls,
        *,
        url: str,
        password: str | None = None,
        algorithm: Literal["flat", "hnsw"] = "flat",
        ef_runtime: int | None = None,
        prefix: str = "",
    ) -> RedisStore:
        redis = _import()
        try:
            client = redis.Redis.from_url(
                url, protocol=3, decode_responses=False, socket_connect_timeout=5, socket_timeout=60
            )
        except (ValueError, redis.exceptions.RedisError) as exc:
            raise StoreError(f"redis: invalid url ({type(exc).__name__})") from None  # the url may hold a password
        kwargs = client.connection_pool.connection_kwargs
        if kwargs.get("protocol") != 3:  # URL options override keyword arguments
            raise StoreError("redis: the store needs the RESP3 protocol; remove 'protocol' from the url")
        if password:
            kwargs["password"] = password  # the secret replaces any password in the url
        return cls(client, algorithm=algorithm, ef_runtime=ef_runtime, prefix=prefix)

    # ---- helpers ------------------------------------------------------------------------------

    def _physical(self, name: str) -> str:
        check_collection_name(name)
        physical = self._prefix + name
        if not set(physical) <= _PREFIX_CHARS:
            raise StoreError(
                f"collection name {physical!r} contains characters that are not allowed in a Redis index name"
            )
        return physical

    def _options(self) -> dict[str, Any]:
        return {"algorithm": self._algorithm, "tag_encoding": TAG_ENCODING_VERSION}

    def _exec(self, what: str, *args: Any) -> Any:
        try:
            return self._client.execute_command(*args)
        except self._rx.ResponseError as exc:
            if _is_missing_index(exc):
                raise CollectionNotFound(f"redis: {what}: index does not exist") from exc
            raise StoreError(f"redis: {what} failed: {exc}") from exc
        except self._rx.RedisError as exc:
            raise StoreError(f"redis: {what} failed: {exc}{_AUTH_HINT if _is_auth_failure(exc) else ''}") from exc

    # catalog storage -------------------------------------------------------------------------

    def _catalog_key(self, name: str) -> str:
        return CATALOG_KEY_PREFIX + self._physical(name)

    def _catalog_read(self, name: str) -> str | None:
        try:
            raw = self._client.get(self._catalog_key(name))
        except self._rx.RedisError as exc:
            raise StoreError(f"redis: could not read the catalog for {name}: {exc}") from exc
        return None if raw is None else _s(raw)

    def _catalog_write(self, name: str, text: str) -> None:
        try:
            self._client.set(self._catalog_key(name), text)
        except self._rx.RedisError as exc:
            raise StoreError(f"redis: could not write the catalog for {name}: {exc}") from exc

    def _catalog_remove(self, name: str) -> None:
        try:
            self._client.delete(self._catalog_key(name))
        except self._rx.RedisError as exc:
            raise StoreError(f"redis: could not remove the catalog for {name}: {exc}") from exc

    @contextlib.contextmanager
    def _locked(self, physical: str) -> Iterator[None]:
        lock = self._client.lock(LOCK_KEY_PREFIX + physical, timeout=LOCK_SECONDS, blocking_timeout=60)
        try:
            acquired = lock.acquire()
        except self._rx.RedisError as exc:
            raise StoreError(f"redis: could not take the collection lock: {exc}") from exc
        if not acquired:
            raise StoreError(f"redis: timed out waiting for another worker to finish changing {physical}")
        try:
            yield
        finally:
            with contextlib.suppress(self._rx.RedisError):
                lock.release()

    # index inspection -------------------------------------------------------------------------

    def _ft_info(self, physical: str) -> dict[str, Any] | None:
        try:
            return _text_keys(self._exec("read the index", "FT.INFO", physical))
        except CollectionNotFound:
            return None

    @staticmethod
    def _attributes(info: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        attrs: dict[str, dict[str, Any]] = {}
        for raw in info.get("attributes") or []:
            attr = _text_keys(raw)
            attrs[_s(attr["identifier"])] = attr
        return attrs

    def _shape(self, name: str, info: Mapping[str, Any]) -> tuple[int, Metric, str]:
        vec = self._attributes(info).get(VECTOR_FIELD)
        if vec is None or _s(vec.get("type")) != "VECTOR":
            raise StoreError(f"redis index for {name} was not created by RAGCentre (no {VECTOR_FIELD} field)")
        metric = _METRIC_FROM_SERVER.get(_s(vec.get("distance_metric")))
        if metric is None:
            raise StoreError(f"redis index for {name} uses an unsupported distance metric")
        return int(vec["dim"]), metric, _s(vec.get("algorithm")).lower()

    def _wait_indexed(self, physical: str) -> None:
        """Wait for background indexing (after creating over existing keys, or adding a field).

        A timeout is not an error: the core rewrites every record when a field is added.
        """
        deadline = time.monotonic() + INDEX_WAIT_SECONDS
        while time.monotonic() < deadline:
            info = self._ft_info(physical)
            if info is None or int(_s(info.get("indexing", 0))) == 0:
                return
            time.sleep(0.05)

    def _entry(self, name: str) -> CatalogEntry:
        check_collection_name(name)
        entry = self._catalog.get(name)
        if entry is None:
            raise CollectionNotFound(f"collection {name} does not exist")
        return entry

    @staticmethod
    def _schema_args(fields: Mapping[str, FieldType]) -> list[str]:
        args: list[str] = []
        for fname, ftype in fields.items():
            args += [fname, "TAG", "CASESENSITIVE"] if ftype in ("str", "bool") else [fname, "NUMERIC"]
        return args

    def _purge(self, physical: str, name: str) -> None:
        """Forget a half-created collection: its catalog entry and any records under its prefix."""
        pattern = physical + ":*"  # the characters of ``physical`` are restricted, so this is not a glob
        try:
            batch: list[Any] = []
            for key in self._client.scan_iter(match=pattern, count=1000):
                batch.append(key)
                if len(batch) >= 1000:
                    self._client.delete(*batch)
                    batch = []
            if batch:
                self._client.delete(*batch)
        except self._rx.RedisError as exc:
            raise StoreError(f"redis: could not clean up {name}: {exc}") from exc
        self._catalog.drop(name)

    # ---- VectorStore --------------------------------------------------------------------------

    def healthcheck(self) -> None:
        try:
            self._client.ping()
        except self._rx.RedisError as exc:
            hint = _AUTH_HINT if _is_auth_failure(exc) else ""
            raise StoreError(f"redis is not reachable or refused the connection: {exc}{hint}") from exc
        try:
            self._client.execute_command("FT._LIST")
        except self._rx.RedisError as exc:
            raise StoreError(
                f"redis is reachable but its search module is not available ({exc}); the store needs "
                "Redis 8 or Redis Stack"
            ) from exc

    def ensure_collection(self, spec: CollectionSpec) -> None:
        check_spec(spec, self.capabilities)
        for fname, ftype in spec.fields.items():
            if not FIELD_RE.fullmatch(fname) or fname.startswith(RESERVED_FIELD_PREFIX):
                raise StoreError(f"field name {fname!r} cannot be a Redis search field")
            if fname == "document_id" and ftype != "str":
                raise StoreError("document_id must be a str field")
        physical = self._physical(spec.name)
        with self._locked(physical):
            info = self._ft_info(physical)
            if info is None and self._catalog.get(spec.name) is not None:
                self._purge(physical, spec.name)  # a creation that never finished, or an index dropped by hand
            if info is not None:
                dim, metric, algorithm = self._shape(spec.name, info)
                if dim != spec.dim or metric != spec.metric:
                    raise CollectionMismatch(
                        f"collection {spec.name} exists with dim={dim}, metric={metric}; "
                        f"requested dim={spec.dim}, metric={spec.metric}"
                    )
                if algorithm != self._algorithm and self._catalog.get(spec.name) is None:
                    raise CollectionMismatch(
                        f"collection {spec.name} was built with algorithm {algorithm}; this store is configured "
                        f"with {self._algorithm} (create a new store provider to change it)"
                    )
            entry = self._catalog.ensure(spec, self._options())
            wanted: dict[str, FieldType] = {"document_id": "str", **entry.fields}
            if info is None:
                self._create_index(physical, entry, wanted)
                return
            present = self._attributes(info)
            missing: dict[str, FieldType] = {}
            for fname, ftype in wanted.items():
                attr = present.get(fname)
                if attr is None:
                    missing[fname] = ftype
                elif _s(attr.get("type")) != ("TAG" if ftype in ("str", "bool") else "NUMERIC"):
                    raise StoreError(
                        f"collection {spec.name}: field {fname} is indexed as {_s(attr.get('type'))}, not {ftype}"
                    )
            if missing:
                for fname, ftype in missing.items():
                    self._add_field(physical, fname, ftype)
                self._wait_indexed(physical)

    def _create_index(self, physical: str, entry: CatalogEntry, wanted: Mapping[str, FieldType]) -> None:
        self._exec(
            f"create index {physical}",
            "FT.CREATE", physical, "ON", "HASH", "PREFIX", "1", physical + ":",
            "SCHEMA", VECTOR_FIELD, "VECTOR", self._algorithm.upper(), "6",
            "TYPE", "FLOAT32", "DIM", str(entry.dim), "DISTANCE_METRIC", _METRICS[entry.metric],
            *self._schema_args(wanted),
        )  # fmt: skip
        self._wait_indexed(physical)

    def _add_field(self, physical: str, fname: str, ftype: FieldType) -> None:
        try:
            self._exec(f"add field {fname}", "FT.ALTER", physical, "SCHEMA", "ADD", *self._schema_args({fname: ftype}))
        except StoreError as exc:
            if "duplicate field" not in str(exc).lower():
                raise

    def collection_info(self, name: str) -> CollectionInfo | None:
        physical = self._physical(name)
        info = self._ft_info(physical)
        if info is None:
            return None
        dim, metric, _ = self._shape(name, info)
        return CollectionInfo(dim=dim, metric=metric, count=int(_s(info.get("num_docs", 0))))

    def drop_collection(self, name: str) -> None:
        physical = self._physical(name)
        with self._locked(physical):
            with contextlib.suppress(CollectionNotFound):
                self._exec(f"drop index {physical}", "FT.DROPINDEX", physical, "DD")
            self._catalog.drop(name)

    def upsert(self, name: str, records: Sequence[Record]) -> None:
        if len(records) > self.capabilities.max_batch:
            raise StoreError(f"batch of {len(records)} exceeds max_batch {self.capabilities.max_batch}")
        entry = self._entry(name)
        physical = self._physical(name)
        fields: dict[str, FieldType] = {"document_id": "str", **entry.fields}
        if not records:
            return
        pipe = self._client.pipeline(transaction=True)
        for rec in records:
            if len(rec.vector) != entry.dim:
                raise StoreError(f"record {rec.id} has dimension {len(rec.vector)}; collection {name} has {entry.dim}")
            try:
                blob = struct.pack(f"<{entry.dim}f", *rec.vector)
            except (struct.error, OverflowError) as exc:
                raise StoreError(f"record {rec.id} has a vector that does not fit FLOAT32: {exc}") from exc
            mapping: dict[str, str | bytes] = {VECTOR_FIELD: blob}
            for fname, ftype in fields.items():
                if fname not in rec.metadata:
                    continue
                value = rec.metadata[fname]
                if not type_ok(value, ftype):
                    raise StoreError(f"record {rec.id}: field {fname} must be {ftype}, got {type(value).__name__}")
                mapping[fname] = _tag_value(value, ftype) if ftype in ("str", "bool") else _number(value)
            key = f"{physical}:{rec.id}"
            pipe.delete(key)  # replace the record completely, metadata included
            pipe.hset(key, mapping=mapping)
        try:
            pipe.execute()
        except self._rx.RedisError as exc:
            raise StoreError(f"redis: upsert on {name} failed: {exc}") from exc

    def delete(self, name: str, ids: Sequence[str]) -> None:
        physical = self._physical(name)
        keys = [f"{physical}:{i}" for i in ids]
        for start in range(0, len(keys), 1000):
            try:
                self._client.delete(*keys[start : start + 1000])
            except self._rx.RedisError as exc:
                raise StoreError(f"redis: delete on {name} failed: {exc}") from exc

    def query(self, name: str, vector: Sequence[float], k: int, filter: Filter | None = None) -> list[Match]:
        entry = self._entry(name)
        if len(vector) != entry.dim:
            raise StoreError(f"query vector has dimension {len(vector)}; collection {name} has {entry.dim}")
        if k < 1:
            return []
        physical = self._physical(name)
        try:
            blob = struct.pack(f"<{entry.dim}f", *vector)
        except (struct.error, OverflowError) as exc:
            raise StoreError(f"query vector does not fit FLOAT32: {exc}") from exc
        expr = "*" if filter is None else f"({compile_filter(filter, {'document_id': 'str', **entry.fields})})"
        params: list[Any] = ["B", blob]
        knn = f"[KNN {k} @{VECTOR_FIELD} $B"
        if entry.options.get("algorithm") == "hnsw":
            knn += " EF_RUNTIME $EF"
            params += ["EF", self._ef_runtime or DEFAULT_HNSW_EF_RUNTIME]
        knn += f" AS {SCORE_FIELD}]"
        reply = self._exec(
            f"query on {name}",
            "FT.SEARCH", physical, f"{expr}=>{knn}",
            "PARAMS", str(len(params)), *params,
            "RETURN", "1", SCORE_FIELD, "LIMIT", "0", str(k), "DIALECT", str(DIALECT),
        )  # fmt: skip
        return parse_matches(reply, physical + ":", entry.metric)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()
            self._client.connection_pool.disconnect()


# ---- registration ---------------------------------------------------------------------------


class RedisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis URL (redis://, rediss:// or unix://); it may carry credentials, or use the password secret",
        json_schema_extra={"x-url-schemes": [scheme.removesuffix("://") for scheme in _SCHEMES]},
    )
    algorithm: Literal["flat", "hnsw"] = Field(
        default="flat",
        description="Vector index: flat (exact, fine below about 1M vectors) or hnsw (approximate). Set once",
    )
    ef_runtime: int | None = Field(
        default=None,
        ge=1,
        description=f"HNSW search-time ef (hnsw only; default {DEFAULT_HNSW_EF_RUNTIME}; the server's own default is 10)",
    )
    prefix: str = Field(default="", description="Prefix of every index and key this store creates")

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        if not v.startswith(_SCHEMES):
            raise ValueError("url must start with redis://, rediss:// or unix://")
        if "protocol=" in urlsplit(v).query:
            raise ValueError("the store manages the protocol itself; remove 'protocol' from the url")
        return v

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        if not set(v) <= _PREFIX_CHARS:
            raise ValueError("prefix may contain only letters, digits, '_', '.', ':' and '-'")
        return v

    @model_validator(mode="after")
    def _ef_needs_hnsw(self) -> RedisConfig:
        if self.ef_runtime is not None and self.algorithm != "hnsw":
            raise ValueError("ef_runtime applies only to algorithm 'hnsw'")
        return self


def _factory(config: BaseModel, secrets: dict[str, str], data_dir: Path) -> RedisStore:
    assert isinstance(config, RedisConfig)
    return RedisStore.connect(
        url=config.url,
        password=secrets.get("password") or None,
        algorithm=config.algorithm,
        ef_runtime=config.ef_runtime,
        prefix=config.prefix,
    )


STORE_TYPE = VectorStoreType(
    type="redis",
    label="Redis",
    description="Redis 8 or Redis Stack (search module): an index per collection. Needs the optional 'redis' extra.",
    config_model=RedisConfig,
    secret_keys=("password",),
    capabilities=RedisStore.capabilities,
    factory=_factory,
    immutable_fields=("algorithm",),
)
