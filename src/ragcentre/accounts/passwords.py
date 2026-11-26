"""Password hashing and username/password validation.

Passwords are hashed with Argon2id (PHC string, self-describing parameters and salt). Usernames are
normalized to ASCII lowercase. These are pure functions with no database access so they are cheap to
test and impossible to use with a transaction open.
"""

from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ragcentre.errors import ValidationFailed

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,63}$")
MIN_PASSWORD_CHARS = 15
MAX_PASSWORD_BYTES = 128

_hasher = PasswordHasher()


def normalize_username(raw: str) -> str:
    return raw.strip().lower()


def validate_username(username: str) -> str:
    """Return the normalized username, or raise ``ValidationFailed``."""
    normalized = normalize_username(username)
    if not USERNAME_RE.match(normalized):
        raise ValidationFailed(
            "Usernames are 3-64 characters, lowercase letters, digits, '.', '_' or '-', "
            "and start with a letter or digit."
        )
    return normalized


def validate_password(password: str) -> None:
    """Reject passwords that are too short or too large; permit spaces and any Unicode."""
    if len(password) < MIN_PASSWORD_CHARS:
        raise ValidationFailed(f"Passwords must be at least {MIN_PASSWORD_CHARS} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValidationFailed(f"Passwords must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 encoded.")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
