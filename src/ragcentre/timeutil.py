"""UTC ISO-8601 timestamps with a fixed width, so they sort correctly as strings in SQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_FMT)


def now_iso() -> str:
    return iso(utcnow())


def after(seconds: float, start: datetime | None = None) -> str:
    return iso((start or utcnow()) + timedelta(seconds=seconds))


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
