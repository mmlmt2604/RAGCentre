"""Google Drive connector: a service account follows the Drive change feed and pulls files.

Scope is either ``{"shared_drive_id": "..."}`` (a shared drive) or ``{"folder_id": "..."}`` (direct
children of a folder). Native Google Docs/Sheets/Slides are exported (plain text / CSV / PDF); other
files are downloaded verbatim. Auth is the JWT-bearer service-account flow.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Mapping
from typing import Any, cast

import httpx
from pydantic import Field

from ragcentre.connectors._http import TokenCache, request, request_json
from ragcentre.connectors.base import FetchedFile, RemoteItem, ScanResult
from ragcentre.errors import ExecutionError
from ragcentre.logs import register_secret
from ragcentre.providers.base import BuildEnv, ProviderConfig, ProviderTypeSpec, invalid_response

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_ROOT = "https://www.googleapis.com/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# Native Google MIME type -> Drive export target. Markdown is not a Drive export format, so Docs
# become plain text (the built-in parser handles it); Sheets become CSV; Slides become PDF.
EXPORT_MAP = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "application/pdf",
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign_rs256(pem: str, signing_input: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

    key = cast(RSAPrivateKey, serialization.load_pem_private_key(pem.encode(), password=None))
    return key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())


def _service_account_jwt(claims: dict[str, Any], private_key_pem: str) -> str:
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    return f"{header}.{payload}.{_b64url(_sign_rs256(private_key_pem, signing_input))}"


class DriveConfig(ProviderConfig):
    service_account_email: str = Field(description="The service account's client_email (its identity).")
    timeout_seconds: float = Field(default=120, gt=0, le=3600, description="Request timeout in seconds")
    max_concurrency: int = Field(default=4, ge=1, le=64, description="Parallel requests")


class DriveConnector:
    def __init__(self, config: DriveConfig, secrets: dict[str, str], env: BuildEnv) -> None:
        self.config = config
        self.name = env.provider_name
        raw = secrets.get("service_account_key", "")
        try:
            key = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise ExecutionError(
                "secret_missing", "service_account_key is not valid JSON.", retryable=False, provider=self.name
            ) from exc
        if key.get("client_email") != config.service_account_email:
            raise ExecutionError(
                "secret_unbound",
                "service_account_key's client_email does not match the configured service_account_email.",
                retryable=False,
                provider=self.name,
            )
        self._client_email = key.get("client_email", "")
        self._private_key = key.get("private_key", "")
        self._token_uri = key.get("token_uri") or TOKEN_URL
        self.client = env.http(timeout=httpx.Timeout(config.timeout_seconds))
        self._token = TokenCache()
        self._native_mime: dict[str, str] = {}

    def _fetch_token(self) -> tuple[str, float]:
        now = int(time.time())
        assertion = _service_account_jwt(
            {"iss": self._client_email, "scope": SCOPE, "aud": self._token_uri, "iat": now, "exp": now + 3600},
            self._private_key,
        )
        body = request_json(
            self.client,
            "POST",
            self._token_uri,
            name=self.name,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        )
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise invalid_response(self.name, "token endpoint returned no access_token")
        register_secret(token)
        return token, float(body.get("expires_in", 3600))

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.get(self._fetch_token)}"}

    def check(self) -> None:
        self._auth()
        request(self.client, "GET", f"{DRIVE_ROOT}/about", name=self.name, headers=self._auth(), params={"fields": "user"})

    def scan(self, scope: Mapping[str, Any], cursor: str | None) -> ScanResult:
        shared_drive_id = scope.get("shared_drive_id")
        folder_id = scope.get("folder_id")
        if cursor is None:
            body = request_json(
                self.client, "GET", f"{DRIVE_ROOT}/changes/getStartPageToken",
                name=self.name, headers=self._auth(), params={"supportsAllDrives": "true"},
            )
            cursor = body.get("startPageToken") if isinstance(body, dict) else None
            if not cursor:
                raise invalid_response(self.name, "getStartPageToken returned no startPageToken")
        body = request_json(
            self.client, "GET", f"{DRIVE_ROOT}/changes",
            name=self.name, headers=self._auth(),
            params={
                "pageToken": cursor,
                "pageSize": "1000",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
            },
        )
        items: list[RemoteItem] = []
        for change in body.get("changes", []) if isinstance(body, dict) else []:
            if not isinstance(change, dict) or change.get("changeType") != "file":
                continue
            file = change.get("file") or {}
            file_id = file.get("id") or change.get("fileId")
            if not isinstance(file_id, str):
                continue
            if change.get("removed"):
                items.append(
                    RemoteItem(
                        remote_id=file_id, name=file_id, mime_type=None, size=None, version=None,
                        modified_at=None, deleted=True, path=None,
                    )
                )
                continue
            if shared_drive_id and file.get("driveId") != shared_drive_id:
                continue
            if folder_id and folder_id not in (file.get("parents") or []):
                continue
            mime = file.get("mimeType") or ""
            if mime == "application/vnd.google-apps.folder" or mime == "application/vnd.google-apps.shortcut":
                continue
            name = file.get("name") or file_id
            if mime.startswith("application/vnd.google-apps."):
                export_mime = EXPORT_MAP.get(mime)
                if not export_mime:
                    continue  # unsupported native type; skip rather than fail the run
                self._native_mime[file_id] = mime
                doc_mime = export_mime
            else:
                doc_mime = mime
            items.append(
                RemoteItem(
                    remote_id=file_id,
                    name=name,
                    mime_type=doc_mime,
                    size=file.get("size"),
                    version=file.get("md5Checksum") or file.get("modifiedTime"),
                    modified_at=file.get("modifiedTime"),
                    deleted=False,
                    path=file.get("driveId"),
                )
            )
        return ScanResult(
            items=iter(items),
            next_cursor=body.get("nextPageToken") if isinstance(body, dict) else None,
            full=False,
            resume_cursor=body.get("newStartPageToken") if isinstance(body, dict) else None,
        )

    def fetch(self, scope: Mapping[str, Any], item: RemoteItem) -> FetchedFile:
        native = self._native_mime.get(item.remote_id)
        if native:
            export_mime = EXPORT_MAP.get(native)
            if not export_mime:
                raise invalid_response(self.name, f"cannot export {native}")
            resp = request(
                self.client, "GET", f"{DRIVE_ROOT}/files/{item.remote_id}/export",
                name=self.name, headers=self._auth(), params={"mimeType": export_mime, "alt": "media"},
            )
            content, mime = resp.content, export_mime
        else:
            resp = request(
                self.client, "GET", f"{DRIVE_ROOT}/files/{item.remote_id}",
                name=self.name, headers=self._auth(), params={"alt": "media"},
            )
            content = resp.content
            mime = item.mime_type or (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower() or (
                "application/octet-stream"
            )
        attributes = {
            "source_url": f"https://drive.google.com/file/d/{item.remote_id}/view",
            "file_id": item.remote_id,
            "drive_id": item.path or "",
        }
        return FetchedFile(
            content=content, filename=item.name, mime_type=mime, version=item.version, metadata={}, attributes=attributes
        )

    def close(self) -> None:
        self.client.close()


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> DriveConnector:
    assert isinstance(config, DriveConfig)
    return DriveConnector(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="connector",
    type="gdrive",
    label="Google Drive",
    description="Sync files from a shared drive or folder via a service account's change feed.",
    config_model=DriveConfig,
    fingerprint_fields=("service_account_email",),
    secret_keys=("service_account_key",),
    factory=_factory,
)
