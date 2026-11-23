from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from ragcentre.blobs import BlobStore
from ragcentre.errors import ExecutionError, SecretStorageDisabled
from ragcentre.hashing import sha256_bytes
from ragcentre.logs import redact
from ragcentre.secrets import SecretBox


def test_blob_roundtrip_and_layout(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "blobs")
    sha = store.put(b"hello")
    assert sha == sha256_bytes(b"hello")
    assert store.path(sha) == tmp_path / "blobs" / sha[:2] / sha[2:4] / sha
    assert store.get(sha) == b"hello"
    assert store.put(b"hello") == sha  # idempotent
    assert list(store.iter_all()) == [sha]
    store.delete(sha)
    assert not store.exists(sha)
    with pytest.raises(KeyError):
        store.get(sha)


def test_blob_rejects_non_hash_paths(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    with pytest.raises(ValueError):
        store.path("../../etc/passwd")


def test_json_gz_is_deterministic(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    a = store.put_json_gz({"a": [1, 2]})
    b = store.put_json_gz({"a": [1, 2]})
    assert a == b
    assert store.get_json_gz(a) == {"a": [1, 2]}


@pytest.mark.parametrize("key", [Fernet.generate_key().decode(), "any passphrase will do"])
def test_secret_roundtrip(key: str) -> None:
    box = SecretBox(key)
    assert box.enabled
    token = box.encrypt("sk-live-123456")
    assert b"sk-live" not in token
    assert box.decrypt(token) == "sk-live-123456"
    assert box.resolve("stored", token) == "sk-live-123456"


def test_secret_refused_without_key() -> None:
    box = SecretBox(None)
    assert not box.enabled
    with pytest.raises(SecretStorageDisabled) as exc:
        box.encrypt("x")
    assert exc.value.error_code.value == "secret_storage_disabled"
    with pytest.raises(SecretStorageDisabled):
        box.decrypt(b"x")


def test_wrong_key_cannot_decrypt() -> None:
    token = SecretBox("one").encrypt("v")
    with pytest.raises(ExecutionError):
        SecretBox("two").decrypt(token)


def test_env_secret_and_redaction() -> None:
    box = SecretBox(None)
    value = box.resolve("env", "RAGCENTRE_PROVIDER_MY_KEY", env={"RAGCENTRE_PROVIDER_MY_KEY": "very-secret-value"})
    assert value == "very-secret-value"
    assert redact("error: token very-secret-value rejected") == "error: token [REDACTED] rejected"
    with pytest.raises(ExecutionError):
        box.resolve("env", "MISSING", env={})
