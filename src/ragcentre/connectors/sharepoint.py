"""SharePoint / OneDrive connector over the Microsoft Graph delta API.

Client-credentials (app-only) auth, one ``drive`` per source. The scope is
``{"drive_id": "...", "folder": "optional/path/prefix"}``; the ``folder`` filter is applied
client-side against each item's ``parentReference.path``.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import Field

from ragcentre.connectors._http import TokenCache, request, request_json
from ragcentre.connectors.base import FetchedFile, RemoteItem, ScanResult
from ragcentre.errors import ExecutionError
from ragcentre.logs import register_secret
from ragcentre.providers.base import BuildEnv, ProviderConfig, ProviderTypeSpec, invalid_response

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


class GraphConfig(ProviderConfig):
    tenant_id: str = Field(description="Entra (Azure AD) tenant id.")
    client_id: str = Field(description="App registration client id.")
    timeout_seconds: float = Field(default=120, gt=0, le=3600, description="Request timeout in seconds")
    max_concurrency: int = Field(default=4, ge=1, le=64, description="Parallel requests")


class SharePointConnector:
    def __init__(self, config: GraphConfig, secrets: dict[str, str], env: BuildEnv) -> None:
        self.config = config
        self.name = env.provider_name
        self.client_secret = secrets.get("client_secret", "")
        self.client = env.http(timeout=httpx.Timeout(config.timeout_seconds))
        self._token = TokenCache()

    def _fetch_token(self) -> tuple[str, float]:
        body = request_json(
            self.client,
            "POST",
            TOKEN_URL.format(tenant_id=self.config.tenant_id),
            name=self.name,
            data={
                "grant_type": "client_credentials",
                "client_id": self.config.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise invalid_response(self.name, "token endpoint returned no access_token")
        register_secret(token)
        return token, float(body.get("expires_in", 3600))

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.get(self._fetch_token)}"}

    def check(self) -> None:
        # Proves the tenant/client/secret are valid (scope reachability is a source concern).
        self._auth()

    def _matches_folder(self, item: dict[str, Any], folder: str | None) -> bool:
        if not folder:
            return True
        parent = item.get("parentReference") or {}
        path = parent.get("path") or ""
        name = item.get("name") or ""
        full = f"{path.rstrip('/')}/{name}" if path else name
        return f"/{folder.strip('/')}/" in f"/{full.strip('/')}/"

    def scan(self, scope: Mapping[str, Any], cursor: str | None) -> ScanResult:
        drive_id = scope.get("drive_id")
        if not isinstance(drive_id, str) or not drive_id:
            raise ExecutionError(
                "invalid_scope", "SharePoint scope requires a string 'drive_id'.", retryable=False, provider=self.name
            )
        folder = scope.get("folder")
        if folder is not None and not isinstance(folder, str):
            raise ExecutionError("invalid_scope", "'folder' must be a string.", retryable=False, provider=self.name)

        url = cursor if cursor else f"{GRAPH_ROOT}/drives/{drive_id}/root/delta"
        body = request_json(self.client, "GET", url, name=self.name, headers=self._auth())
        items: list[RemoteItem] = []
        for it in body.get("value", []) if isinstance(body, dict) else []:
            if not isinstance(it, dict):
                continue
            remote_id = it.get("id")
            if not isinstance(remote_id, str):
                continue
            if "folder" in it or "package" in it:
                continue
            name = it.get("name") or remote_id
            if "deleted" in it:
                items.append(
                    RemoteItem(
                        remote_id=remote_id, name=name, mime_type=None, size=None, version=None,
                        modified_at=None, deleted=True, path=None,
                    )
                )
                continue
            if not self._matches_folder(it, folder):
                continue
            file = it.get("file") or {}
            items.append(
                RemoteItem(
                    remote_id=remote_id,
                    name=name,
                    mime_type=file.get("mimeType"),
                    size=it.get("size"),
                    version=it.get("cTag") or it.get("eTag"),
                    modified_at=it.get("lastModifiedDateTime"),
                    deleted=False,
                    path=(it.get("parentReference") or {}).get("path"),
                )
            )
        return ScanResult(
            items=iter(items),
            next_cursor=body.get("@odata.nextLink") if isinstance(body, dict) else None,
            full=False,
            resume_cursor=body.get("@odata.deltaLink") if isinstance(body, dict) else None,
        )

    def fetch(self, scope: Mapping[str, Any], item: RemoteItem) -> FetchedFile:
        drive_id = scope.get("drive_id")
        meta = request_json(
            self.client, "GET", f"{GRAPH_ROOT}/drives/{drive_id}/items/{item.remote_id}",
            name=self.name, headers=self._auth(),
        )
        download_url = meta.get("@microsoft.graph.downloadUrl") if isinstance(meta, dict) else None
        if not isinstance(download_url, str) or not download_url:
            raise invalid_response(self.name, f"item {item.remote_id} has no download URL")
        register_secret(download_url)
        resp = request(self.client, "GET", download_url, name=self.name)
        mime = item.mime_type or (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not mime or mime == "application/octet-stream":
            mime = mimetypes.guess_type(item.name)[0] or "application/octet-stream"
        attributes = {
            "source_url": meta.get("webUrl", "") if isinstance(meta, dict) else "",
            "drive_id": drive_id,
            "item_id": item.remote_id,
            "path": item.path or "",
        }
        return FetchedFile(
            content=resp.content,
            filename=item.name,
            mime_type=mime,
            version=item.version,
            metadata={},
            attributes=attributes,
        )

    def close(self) -> None:
        self.client.close()


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> SharePointConnector:
    assert isinstance(config, GraphConfig)
    return SharePointConnector(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="connector",
    type="sharepoint",
    label="SharePoint / OneDrive (Microsoft Graph)",
    description="Sync document libraries via the Graph delta API (app-only client credentials).",
    config_model=GraphConfig,
    fingerprint_fields=("tenant_id", "client_id"),
    secret_keys=("client_secret",),
    factory=_factory,
)
