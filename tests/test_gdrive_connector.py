from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ragcentre.connectors.gdrive import DriveConfig, DriveConnector, _service_account_jwt
from ragcentre.providers.base import BuildEnv

EMAIL = "sa@proj.iam.gserviceaccount.com"


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _env(handler: Any) -> BuildEnv:
    transport = httpx.MockTransport(handler)

    def http(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=transport, **kwargs)

    return BuildEnv("p", "gdrive", http, Path("."), threading.BoundedSemaphore(1))


def _connector(handler: Any, private_key_pem: str) -> DriveConnector:
    config = DriveConfig(service_account_email=EMAIL)
    secret = json.dumps({"client_email": EMAIL, "private_key": private_key_pem, "token_uri": "https://oauth2.googleapis.com/token"})
    return DriveConnector(config, {"service_account_key": secret}, _env(handler))


def test_jwt_shape(private_key_pem: str) -> None:
    jwt = _service_account_jwt({"iss": EMAIL, "scope": "s", "aud": "a", "iat": 1, "exp": 2}, private_key_pem)
    assert jwt.count(".") == 2


def test_scan_and_fetch(private_key_pem: str) -> None:
    changes = {
        "changes": [
            {
                "kind": "drive#change",
                "changeType": "file",
                "fileId": "bin",
                "file": {"id": "bin", "name": "a.txt", "mimeType": "text/plain", "md5Checksum": "h1", "size": "4", "driveId": "sd1"},
            },
            {
                "kind": "drive#change",
                "changeType": "file",
                "fileId": "doc",
                "file": {"id": "doc", "name": "notes", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-01-01T00:00:00Z", "driveId": "sd1"},
            },
            {"kind": "drive#change", "changeType": "file", "fileId": "gone", "removed": True},
        ],
        "newStartPageToken": "st2",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = request.url.query.decode()
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if path.endswith("/changes/getStartPageToken"):
            return httpx.Response(200, json={"startPageToken": "st1"})
        if path.endswith("/changes"):
            return httpx.Response(200, json=changes)
        if path.endswith("/files/bin") and "alt=media" in query:
            return httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"})
        if path.endswith("/files/doc/export"):
            return httpx.Response(200, content=b"plain text", headers={"content-type": "text/plain"})
        return httpx.Response(404)

    conn = _connector(handler, private_key_pem)
    result = conn.scan({"shared_drive_id": "sd1"}, None)
    items = list(result.items)
    assert len(items) == 3
    by_id = {i.remote_id: i for i in items}
    assert by_id["bin"].version == "h1"
    assert by_id["doc"].mime_type == "text/plain"  # exported
    assert by_id["gone"].deleted is True
    assert result.full is False
    assert result.resume_cursor == "st2"

    assert conn.fetch({"shared_drive_id": "sd1"}, by_id["bin"]).content == b"hello"
    assert conn.fetch({"shared_drive_id": "sd1"}, by_id["doc"]).content == b"plain text"


def test_email_mismatch_is_rejected(private_key_pem: str) -> None:
    from ragcentre.errors import ExecutionError

    config = DriveConfig(service_account_email="other@proj.iam.gserviceaccount.com")
    secret = json.dumps({"client_email": EMAIL, "private_key": private_key_pem})
    with pytest.raises(ExecutionError):
        DriveConnector(config, {"service_account_key": secret}, _env(lambda r: httpx.Response(404)))
