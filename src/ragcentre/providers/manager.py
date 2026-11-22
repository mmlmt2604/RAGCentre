"""Provider rows, fingerprints, secrets and live provider instances (sections 6, 8, 15)."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ValidationError

from ragcentre.errors import (
    Conflict,
    ExecutionError,
    ImmutableField,
    NotFound,
    ProviderFailed,
    ProviderInUse,
    ValidationFailed,
)
from ragcentre.hashing import new_id, provider_fp
from ragcentre.logs import redact, register_secret
from ragcentre.providers.base import KINDS, BuildEnv, InfoService, ProviderTypeSpec, ServiceInfo
from ragcentre.providers.registry import get_type, provider_types
from ragcentre.stores.base import StoreError
from ragcentre.timeutil import now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext
    from ragcentre.stores.base import VectorStore

log = logging.getLogger("ragcentre.providers")

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SECRET_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

# Providers created automatically on first start. They are marked built-in and cannot be deleted.
BUILTIN_PROVIDERS: dict[str, tuple[str, str]] = {"builtin-text": ("parser", "builtin_text")}


@dataclass(frozen=True)
class ProviderRow:
    id: str
    name: str
    kind: str
    type: str
    config: dict[str, Any]
    fingerprint: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ProviderRow:
        return cls(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            type=row["type"],
            config=json.loads(row["config"]),
            fingerprint=row["fingerprint"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @property
    def builtin(self) -> bool:
        return BUILTIN_PROVIDERS.get(self.name) == (self.kind, self.type)


def _validation(exc: ValidationError, prefix: str = "config") -> ValidationFailed:
    problems = [
        {"path": ".".join([prefix, *(str(p) for p in e.get("loc", ()))]), "message": str(e.get("msg", ""))}
        for e in exc.errors()
    ]
    summary = "; ".join(f"{p['path']}: {p['message']}" for p in problems[:5])
    return ValidationFailed(f"Invalid provider configuration: {summary}", details={"problems": problems})


def fingerprint_for(spec: ProviderTypeSpec, provider_id: str, config: Mapping[str, Any]) -> str:
    if spec.kind == "vector_store":
        # An index in a different store instance is a different index.
        return provider_fp(spec.kind, spec.type, {"id": provider_id})
    return provider_fp(spec.kind, spec.type, {f: config.get(f) for f in spec.fingerprint_fields})


class ProviderManager:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._instances: dict[str, tuple[str, Any]] = {}
        self._retired: list[Any] = []
        self._semaphores: dict[str, tuple[int, threading.BoundedSemaphore]] = {}

    # ---- reading ------------------------------------------------------------------------------

    @property
    def db(self) -> Any:
        return self.ctx.db

    def get(self, provider_id: str) -> ProviderRow:
        row = self.db.one("SELECT * FROM providers WHERE id = ?", (provider_id,))
        if row is None:
            raise NotFound(f"Provider {provider_id} was not found.")
        return ProviderRow.from_row(row)

    def find(self, provider_id: str) -> ProviderRow | None:
        row = self.db.one("SELECT * FROM providers WHERE id = ?", (provider_id,))
        return None if row is None else ProviderRow.from_row(row)

    def by_name(self, name: str) -> ProviderRow | None:
        row = self.db.one("SELECT * FROM providers WHERE name = ?", (name,))
        return None if row is None else ProviderRow.from_row(row)

    def list(self) -> list[ProviderRow]:
        rows = self.db.all("SELECT * FROM providers ORDER BY kind, name")
        return [ProviderRow.from_row(r) for r in rows]

    def spec(self, row: ProviderRow) -> ProviderTypeSpec:
        spec = get_type(row.kind, row.type)
        if spec is None:
            raise ExecutionError(
                "provider_error", f"Provider type {row.kind}/{row.type} is not installed.", provider=row.name
            )
        return spec

    def secret_states(self, provider_id: str, spec: ProviderTypeSpec | None) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        for key in spec.secret_keys if spec else ():
            states[key] = {"source": "env", "set": False, "env_var": None}
        for row in self.db.all("SELECT key, source, value FROM provider_secrets WHERE provider_id = ?", (provider_id,)):
            env_var = bytes(row["value"]).decode("utf-8") if row["source"] == "env" else None
            states[row["key"]] = {"source": row["source"], "set": True, "env_var": env_var}
        return states

    def usage_counts(self) -> dict[str, int]:
        rows = self.db.all(
            "SELECT j.value AS provider_id, COUNT(DISTINCT p.id) AS n "
            "FROM pipelines p, json_each(p.resolved, '$.provider_ids') j "
            "WHERE p.archived = 0 GROUP BY j.value"
        )
        return {r["provider_id"]: int(r["n"]) for r in rows}

    # ---- writing ------------------------------------------------------------------------------

    def create(self, name: str, kind: str, type_: str, config: Mapping[str, Any]) -> ProviderRow:
        if not NAME_RE.match(name):
            raise ValidationFailed(
                "Provider names use lowercase letters, digits and hyphens, start with a letter, "
                "and are at most 63 characters.",
                details={"problems": [{"path": "name", "message": "invalid name"}]},
            )
        if kind not in KINDS:
            raise ValidationFailed(f"Unknown provider kind {kind!r}.")
        spec = get_type(kind, type_)
        if spec is None:
            known = ", ".join(sorted(t for k, t in provider_types() if k == kind))
            raise ValidationFailed(f"Unknown {kind} type {type_!r}. Installed types: {known}.")
        try:
            model = spec.config_model.model_validate(dict(config))
        except ValidationError as exc:
            raise _validation(exc) from None
        stored: dict[str, Any] = {**model.model_dump(mode="json"), **spec.fixed_config}
        provider_id = new_id()
        if spec.captured_fields:
            info = self._capture_info(spec, provider_id, name, model)
            stored["service_name"] = info.name
            stored["service_version"] = info.version
            if "capabilities" in spec.captured_fields:
                stored["capabilities"] = info.capabilities
            if "options_schema" in spec.captured_fields and info.options_schema is not None:
                stored["options_schema"] = info.options_schema
        if self.by_name(name) is not None:
            raise Conflict(f"A provider named {name!r} already exists.")
        now = now_iso()
        fingerprint = fingerprint_for(spec, provider_id, stored)
        try:
            with self.db.write() as conn:
                conn.execute(
                    "INSERT INTO providers (id, name, kind, type, config, fingerprint, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (provider_id, name, kind, type_, json.dumps(stored), fingerprint, now, now),
                )
        except sqlite3.IntegrityError:
            raise Conflict(f"A provider named {name!r} already exists.") from None
        log.info("provider created", extra={"provider": name, "kind": kind, "type": type_})
        return self.get(provider_id)

    def _capture_info(self, spec: ProviderTypeSpec, provider_id: str, name: str, model: BaseModel) -> ServiceInfo:
        """Call ``/info`` (without credentials) to pin the service name and version."""
        env = self._env(provider_id, name, model)
        service = None
        try:
            service = spec.factory(model, {}, env)
            info = cast(InfoService, service).info()
        except (ExecutionError, StoreError) as exc:
            message = exc.message if isinstance(exc, ExecutionError) else str(exc)
            raise ProviderFailed(
                redact(f"Could not read {name}'s /info to pin its version: {message}"),
                details={"provider": name},
            ) from None
        finally:
            close = getattr(service, "close", None)
            if close is not None:
                close()
        return info

    def _store_options_schema(self, row: ProviderRow, schema: dict[str, Any] | None) -> None:
        """Refresh the options schema captured from the service. It is not part of the fingerprint:
        it only drives pipeline validation and the UI's option form."""
        config = {k: v for k, v in row.config.items() if k != "options_schema"}
        if schema is not None:
            config["options_schema"] = schema
        with self.db.write() as conn:
            conn.execute(
                "UPDATE providers SET config = ?, updated_at = ? WHERE id = ?",
                (json.dumps(config), now_iso(), row.id),
            )

    def update(self, provider_id: str, patch: Mapping[str, Any]) -> ProviderRow:
        row = self.get(provider_id)
        spec = self.spec(row)
        permanent = set(spec.fingerprint_fields) | set(spec.fixed_config) | set(spec.captured_fields)
        for key, value in patch.items():
            if key in permanent and value != row.config.get(key):
                raise ImmutableField(
                    f"{key} is part of {row.name}'s fingerprint and cannot be changed. "
                    "Create a new provider (and a new pipeline) instead.",
                    details={"field": key},
                )
        fields = set(spec.config_model.model_fields)
        merged = {k: v for k, v in row.config.items() if k in fields}
        merged.update({k: v for k, v in patch.items() if k not in permanent or k in fields})
        try:
            model = spec.config_model.model_validate(merged)
        except ValidationError as exc:
            raise _validation(exc) from None
        stored = {**model.model_dump(mode="json")}
        for key in permanent:
            if key in row.config:
                stored[key] = row.config[key]
        assert fingerprint_for(spec, row.id, stored) == row.fingerprint, "operational change altered fingerprint"
        with self.db.write() as conn:
            conn.execute(
                "UPDATE providers SET config = ?, updated_at = ? WHERE id = ?",
                (json.dumps(stored), now_iso(), provider_id),
            )
        self._retire(provider_id)
        return self.get(provider_id)

    def delete(self, provider_id: str) -> None:
        row = self.get(provider_id)
        if row.builtin:
            raise Conflict(f"{row.name} is built in and cannot be deleted.")
        used = self.usage_counts().get(provider_id, 0)
        if used:
            raise ProviderInUse(
                f"{row.name} is used by {used} pipeline{'s' if used != 1 else ''}. Archive them first.",
                details={"pipelines": used},
            )
        with self.db.write() as conn:
            conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        self._retire(provider_id)

    def set_secret(self, provider_id: str, key: str, *, env: str | None = None, value: str | None = None) -> None:
        row = self.get(provider_id)
        spec = self.spec(row)
        if key not in spec.secret_keys:
            allowed = ", ".join(spec.secret_keys) or "none"
            raise ValidationFailed(f"{row.type} providers have no secret {key!r} (secret keys: {allowed}).")
        if (env is None) == (value is None):
            raise ValidationFailed('Send exactly one of {"env": "VAR_NAME"} or {"value": "..."}.')
        if env is not None:
            if not ENV_NAME_RE.match(env):
                raise ValidationFailed("env must be an environment variable name.")
            source, blob = "env", env.encode("utf-8")
        else:
            assert value is not None
            register_secret(value)
            source, blob = "stored", self.ctx.secrets.encrypt(value)
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO provider_secrets (provider_id, key, source, value) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (provider_id, key) DO UPDATE SET source = excluded.source, value = excluded.value",
                (provider_id, key, source, blob),
            )
            conn.execute("UPDATE providers SET updated_at = ? WHERE id = ?", (now_iso(), provider_id))
        self._retire(provider_id)

    def delete_secret(self, provider_id: str, key: str) -> None:
        self.get(provider_id)
        with self.db.write() as conn:
            conn.execute("DELETE FROM provider_secrets WHERE provider_id = ? AND key = ?", (provider_id, key))
            conn.execute("UPDATE providers SET updated_at = ? WHERE id = ?", (now_iso(), provider_id))
        self._retire(provider_id)

    # ---- live instances -----------------------------------------------------------------------

    def semaphore(self, provider_id: str, size: int) -> threading.BoundedSemaphore:
        with self._lock:
            current = self._semaphores.get(provider_id)
            if current is None or current[0] != size:
                current = (size, threading.BoundedSemaphore(size))
                self._semaphores[provider_id] = current
            return current[1]

    def _env(self, provider_id: str, name: str, model: BaseModel) -> BuildEnv:
        size = int(getattr(model, "max_concurrency", 0) or 64)
        return BuildEnv(
            provider_id=provider_id,
            provider_name=name,
            http=self.ctx.http_client_factory,
            data_dir=self.ctx.settings.data_dir,
            semaphore=self.semaphore(provider_id, size),
        )

    def _resolve_secrets(self, provider_id: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for row in self.db.all("SELECT key, source, value FROM provider_secrets WHERE provider_id = ?", (provider_id,)):
            out[row["key"]] = self.ctx.secrets.resolve(row["source"], bytes(row["value"]))
        return out

    def secret_values(self, provider_id: str) -> dict[str, str]:
        """The resolved secrets. For server-side callers only; never put them in a response."""
        return self._resolve_secrets(provider_id)

    def instance(self, provider_id: str) -> Any:
        row = self.get(provider_id)
        with self._lock:
            cached = self._instances.get(provider_id)
            if cached is not None and cached[0] == row.updated_at:
                return cached[1]
        spec = self.spec(row)
        fields = set(spec.config_model.model_fields)
        model = spec.config_model.model_validate({k: v for k, v in row.config.items() if k in fields})
        built = spec.factory(model, self._resolve_secrets(provider_id), self._env(row.id, row.name, model))
        with self._lock:
            cached = self._instances.get(provider_id)
            if cached is not None and cached[0] == row.updated_at:
                self._retired.append(built)
                return cached[1]
            if cached is not None:
                self._retired.append(cached[1])
            self._instances[provider_id] = (row.updated_at, built)
        return built

    def store(self, provider_id: str) -> VectorStore:
        return cast("VectorStore", self.instance(provider_id))

    def _retire(self, provider_id: str) -> None:
        with self._lock:
            cached = self._instances.pop(provider_id, None)
            if cached is not None:
                self._retired.append(cached[1])

    def close(self) -> None:
        with self._lock:
            items = [inst for _, inst in self._instances.values()] + self._retired
            self._instances.clear()
            self._retired = []
        for inst in items:
            close = getattr(inst, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # pragma: no cover - best effort
                    log.warning("error closing provider", exc_info=True)

    # ---- health check -------------------------------------------------------------------------

    def test(self, provider_id: str, *, deep: bool = False) -> dict[str, Any]:
        row = self.get(provider_id)
        result: dict[str, Any] = {
            "ok": False,
            "latency_ms": 0.0,
            "message": None,
            "service": None,
            "dimensions": None,
            "version_drift": None,
        }
        start = time.perf_counter()
        try:
            spec = self.spec(row)
            inst = self.instance(provider_id)
            if spec.captured_fields:
                info = cast(Any, inst).info(fresh=True)
                result["service"] = {"name": info.name, "version": info.version, "mime_types": info.mime_types}
                pinned = str(row.config.get("service_version", ""))
                if row.kind == "vector_store" and not pinned:
                    # HTTP stores created before metadata capture have no version to compare.
                    # Pin the first successful handshake instead of reporting drift from "".
                    with self.db.write() as conn:
                        conn.execute(
                            "UPDATE providers SET config = json_set(config, '$.service_name', ?, "
                            "'$.service_version', ?, '$.capabilities', json(?)) WHERE id = ? AND updated_at = ?",
                            (info.name, info.version, json.dumps(info.capabilities), row.id, row.updated_at),
                        )
                    pinned = info.version
                if "options_schema" in spec.captured_fields and info.options_schema != row.config.get("options_schema"):
                    self._store_options_schema(row, info.options_schema)  # the service's options changed
                if "capabilities" in spec.captured_fields and info.capabilities != row.config.get("capabilities"):
                    with self.db.write() as conn:
                        conn.execute(
                            "UPDATE providers SET config = json_set(config, '$.capabilities', json(?)) "
                            "WHERE id = ? AND updated_at = ?",
                            (json.dumps(info.capabilities), row.id, row.updated_at),
                        )
                if row.kind == "vector_store":
                    inst.healthcheck()
                if info.version != pinned:
                    result["version_drift"] = {"pinned": pinned, "current": info.version}
                    result["message"] = (
                        f"{row.name} now reports version {info.version}; it was pinned at {pinned} when created."
                    )
            elif row.kind == "parser":
                from ragcentre.providers.parsers.builtin_text import IMPL_VERSION, MIME_TYPES

                result["service"] = {"name": row.type, "version": str(IMPL_VERSION), "mime_types": list(MIME_TYPES)}
            elif row.kind == "embedder":
                vectors = inst.embed(["RAGCentre connectivity test"], "query")
                result["dimensions"] = len(vectors[0]) if vectors else None
            elif row.kind == "reranker":
                inst.rerank("connectivity test", ["first document", "second document"], 2)
            elif row.kind == "vector_store":
                inst.healthcheck()
            result["ok"] = result["version_drift"] is None
            if deep and row.kind == "parser" and row.type == "http":
                # Parse a sample document and look at the IR, not just /info.
                from ragcentre.providers.parsers.check import check_parser

                report = check_parser(cast(Any, inst).client, cast(Any, inst).base_url)
                for finding in report.findings:
                    finding.message = redact(finding.message)
                result["check"] = report.as_dict()  # only a deep test has this key
                result["ok"] = bool(result["ok"] and report.ok)
                if not report.ok and result["message"] is None:
                    errors = [f.message for f in report.findings if f.level == "error"]
                    result["message"] = redact("; ".join(errors))[:500]
            elif deep and row.kind == "vector_store":
                # Write, query, filter and delete against a temporary collection, not just healthcheck.
                from ragcentre.stores.check import check_store

                store_report = check_store(cast("VectorStore", inst), store_type=row.type)
                for store_finding in store_report.findings:
                    store_finding.message = redact(store_finding.message)
                result["check"] = store_report.as_dict()  # only a deep test has this key
                result["ok"] = bool(result["ok"] and store_report.ok)
                if not store_report.ok and result["message"] is None:
                    errors = [f.message for f in store_report.findings if f.level == "error"]
                    result["message"] = redact("; ".join(errors))[:500]
        except ExecutionError as exc:
            result["message"] = redact(exc.message)
        except Exception as exc:
            result["message"] = redact(f"{type(exc).__name__}: {exc}")
        result["latency_ms"] = round((time.perf_counter() - start) * 1000, 2)
        return result


def ensure_builtin_providers(ctx: AppContext) -> None:
    for name, (kind, type_) in BUILTIN_PROVIDERS.items():
        if ctx.providers.by_name(name) is None:
            with contextlib.suppress(Conflict):  # created concurrently by another process
                ctx.providers.create(name, kind, type_, {})
