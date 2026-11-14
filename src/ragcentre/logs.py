"""JSON-lines logging (section 17) with secret redaction."""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import UTC, datetime
from typing import Any

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}

_redact_lock = threading.Lock()
_known_secrets: set[str] = set()


_factory_installed = False


def _install_record_factory() -> None:
    """Redact every log record when it is created, so all handlers see redacted text."""
    global _factory_installed
    if _factory_installed:
        return
    _factory_installed = True
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if _known_secrets:
            try:
                message = record.getMessage()
            except Exception:  # pragma: no cover - malformed format string
                return record
            redacted = redact(message)
            if redacted != message:
                record.msg, record.args = redacted, None
        return record

    logging.setLogRecordFactory(factory)


def register_secret(value: str) -> None:
    """Remember a secret value so the redactor can remove it from logs and stored errors."""
    if value and len(value) >= 4:
        with _redact_lock:
            _known_secrets.add(value)
            _install_record_factory()


def redact(text: str) -> str:
    with _redact_lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, "[REDACTED]")
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger("ragcentre")
    root.setLevel(level)
    if not any(getattr(h, "_ragcentre", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        handler._ragcentre = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.propagate = True
