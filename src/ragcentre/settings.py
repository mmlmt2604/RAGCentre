"""Environment-driven settings (IMPLEMENTATION_PLAN.md section 17)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _opt(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name)
    return raw if raw else None


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("./data")
    host: str = "127.0.0.1"
    port: int = 8700
    workers: int = 4
    api_token: str | None = None
    secret_key: str | None = None
    max_upload_mb: int = 200
    max_attempts: int = 5
    log_level: str = "INFO"
    # Queue timings. Not environment settings in the plan; tests shorten them.
    lease_seconds: float = 60.0
    heartbeat_seconds: float = 15.0
    idle_sleep_seconds: float = 0.5
    max_idle_sleep_seconds: float = 2.0
    shutdown_grace_seconds: float = 30.0
    secret_env_prefixes: tuple[str, ...] = ("RAGCENTRE_PROVIDER_",)
    egress_allow: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        return cls(
            data_dir=Path(e.get("RAGCENTRE_DATA_DIR") or "./data"),
            host=e.get("RAGCENTRE_HOST") or "127.0.0.1",
            port=_int(e, "RAGCENTRE_PORT", 8700),
            workers=_int(e, "RAGCENTRE_WORKERS", 4),
            api_token=_opt(e, "RAGCENTRE_API_TOKEN"),
            secret_key=_opt(e, "RAGCENTRE_SECRET_KEY"),
            secret_env_prefixes=tuple(
                part.strip()
                for part in e.get("RAGCENTRE_SECRET_ENV_PREFIXES", "RAGCENTRE_PROVIDER_").split(",")
                if part.strip()
            ),
            egress_allow=tuple(
                part.strip().lower().rstrip(".")
                for part in e.get("RAGCENTRE_EGRESS_ALLOW", "").split(",")
                if part.strip()
            ),
            max_upload_mb=_int(e, "RAGCENTRE_MAX_UPLOAD_MB", 200),
            max_attempts=_int(e, "RAGCENTRE_MAX_ATTEMPTS", 5),
            log_level=(e.get("RAGCENTRE_LOG_LEVEL") or "INFO").upper(),
        )

    def with_(self, **changes: object) -> Settings:
        return replace(self, **changes)  # type: ignore[arg-type]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ragcentre.db"

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "cache.db"

    @property
    def vectors_path(self) -> Path:
        return self.data_dir / "vectors.db"

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024
