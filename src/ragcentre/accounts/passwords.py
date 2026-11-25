"""Bounded Argon2id work, executed from synchronous handlers rather than the event loop."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.profiles import RFC_9106_LOW_MEMORY

from ragcentre.errors import ErrorCode, RagError, ValidationFailed

_hasher = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)
_slots = threading.BoundedSemaphore(2)
_dummy: str | None = None
_dummy_lock = threading.Lock()


class TooManyAttempts(RagError):
    code = ErrorCode.RATE_LIMITED
    status = 429


@contextmanager
def _slot() -> Iterator[None]:
    if not _slots.acquire(blocking=False):
        raise TooManyAttempts("Authentication is busy. Try again shortly.")
    try:
        yield
    finally:
        _slots.release()


def validate_password(password: str) -> None:
    if not 15 <= len(password) <= 128 or len(password.encode("utf-8")) > 512:
        raise ValidationFailed("Passwords must contain 15–128 characters and at most 512 UTF-8 bytes.")


def hash_password(password: str) -> str:
    validate_password(password)
    with _slot():
        return _hasher.hash(password)


def verify_password(encoded: str | None, password: str) -> bool:
    global _dummy
    with _slot():
        if encoded is None:
            with _dummy_lock:
                if _dummy is None:
                    _dummy = _hasher.hash("unusable dummy account password")
            encoded = _dummy
            known = False
        else:
            known = True
        try:
            valid = _hasher.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False
        return bool(valid and known)


def needs_rehash(encoded: str) -> bool:
    return _hasher.check_needs_rehash(encoded)


class LoginLimiter:
    """Bounded single-API-process limiter; worker processes do not authenticate browsers."""

    def __init__(self) -> None:
        self._buckets: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, *keys: str) -> None:
        now = monotonic()
        with self._lock:
            for key in keys:
                start, count = self._buckets.get(key, (now, 0))
                if now - start >= 60:
                    start, count = now, 0
                self._buckets[key] = (start, count + 1)
                self._buckets.move_to_end(key)
                while len(self._buckets) > 4096:
                    self._buckets.popitem(last=False)
                if count >= 20:
                    raise TooManyAttempts("Too many authentication attempts. Try again in a minute.")
