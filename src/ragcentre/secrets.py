"""Secret storage (section 15).

A provider secret is either an environment-variable reference (read at use time) or a value
encrypted with Fernet under ``RAGCENTRE_SECRET_KEY``. Without that key, storing values is refused
with ``secret_storage_disabled``; environment references still work.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from collections.abc import Mapping
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken

from ragcentre.errors import ErrorCode, ExecutionError, SecretStorageDisabled, ValidationFailed
from ragcentre.logs import register_secret

SecretSource = Literal["env", "stored"]


def _fernet_key(secret_key: str) -> bytes:
    """Use the value directly when it is a Fernet key; otherwise derive one with SHA-256."""
    raw = secret_key.strip().encode("utf-8")
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return raw
    except (binascii.Error, ValueError):
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


class SecretBox:
    def __init__(self, secret_key: str | None, *, env_prefixes: tuple[str, ...] = ("RAGCENTRE_PROVIDER_",)) -> None:
        self._env_prefixes = tuple(prefix for prefix in env_prefixes if prefix)
        self._fernet = Fernet(_fernet_key(secret_key)) if secret_key else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def _require(self) -> Fernet:
        if self._fernet is None:
            raise SecretStorageDisabled(
                "Storing secret values is disabled because RAGCENTRE_SECRET_KEY is not set. "
                "Set it, or reference an environment variable instead."
            )
        return self._fernet

    def encrypt(self, value: str) -> bytes:
        return self._require().encrypt(value.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        try:
            return self._require().decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise ExecutionError(
                ErrorCode.SECRET_STORAGE_DISABLED.value,
                "A stored secret cannot be decrypted with the current RAGCENTRE_SECRET_KEY.",
            ) from exc

    def validate_env(self, name: str) -> None:
        if name.upper() in {"RAGCENTRE_API_TOKEN", "RAGCENTRE_SECRET_KEY"} or not name.startswith(self._env_prefixes):
            raise ValidationFailed("Provider secret environment variable is outside the allowed prefixes.")

    def resolve(self, source: str, value: bytes | str, env: Mapping[str, str] | None = None) -> str:
        """Return the secret's value and register it with the log redactor."""
        if source == "env":
            name = value.decode("utf-8") if isinstance(value, bytes) else value
            try:
                self.validate_env(name)
            except ValidationFailed:
                raise ExecutionError(
                    "secret_not_allowed", "Provider secret environment reference is disallowed."
                ) from None
            environ = os.environ if env is None else env
            resolved = environ.get(name)
            if resolved is None:
                raise ExecutionError(
                    "secret_missing", f"Environment variable {name} referenced by a provider secret is not set."
                )
        elif source == "stored":
            token = value if isinstance(value, bytes) else value.encode("utf-8")
            resolved = self.decrypt(token)
        else:
            raise ValueError(f"unknown secret source {source!r}")
        register_secret(resolved)
        return resolved
