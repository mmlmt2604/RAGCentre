"""S3 / S3-compatible connector: list and fetch objects with a hand-rolled GET-only SigV4 signer.

No ``boto3``: signing is a small, well-specified amount of code and keeps every request inside the
egress-checked ``checked_http_client`` that ``env.http`` returns.
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import Field

from ragcentre.connectors.base import FetchedFile, RemoteItem, ScanResult
from ragcentre.errors import ExecutionError
from ragcentre.providers.base import BuildEnv, ProviderConfig, ProviderTypeSpec, classify_exception, classify_response

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def _sigv4_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    k_date = _hmac(("AWS4" + secret_key).encode(), date_stamp.encode())
    k_region = _hmac(k_date, region.encode())
    k_service = _hmac(k_region, b"s3")
    return _hmac(k_service, b"aws4_request")


def _uri_encode(value: str) -> str:
    return quote(value, safe="-_.~")


def _canonical_query(params: list[tuple[str, str]]) -> str:
    items = sorted((_uri_encode(k), _uri_encode(v)) for k, v in params)
    return "&".join(f"{k}={v}" for k, v in items)


def sign_headers(
    *,
    method: str,
    host: str,
    path: str,
    query: list[tuple[str, str]],
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str | None,
    payload_hash: str,
) -> dict[str, str]:
    """Return the ``Authorization``/``x-amz-*`` headers for a GET with no body."""
    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    canonical_uri = path or "/"
    canonical_query = _canonical_query(query)
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    signature = hmac.new(_sigv4_key(secret_key, date_stamp, region), string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    return headers


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _child_text(elem: ET.Element, name: str) -> str | None:
    for child in elem:
        if _local(child.tag) == name:
            return child.text
    return None


class S3Config(ProviderConfig):
    base_url: str | None = Field(
        default=None, description="S3-compatible endpoint URL; empty for AWS. Also satisfies the egress policy."
    )
    region: str = Field(default="us-east-1", description="AWS region for signing and the default endpoint.")
    path_style: bool = Field(default=False, description="Use bucket-in-path addressing (MinIO/R2 style).")
    timeout_seconds: float = Field(default=120, gt=0, le=3600, description="Request timeout in seconds")
    max_concurrency: int = Field(default=4, ge=1, le=64, description="Parallel requests")


class S3Connector:
    _scheme: str
    _endpoint_host: str | None
    _base_path: str

    def __init__(self, config: S3Config, secrets: dict[str, str], env: BuildEnv) -> None:
        self.config = config
        self.name = env.provider_name
        self.access_key_id = secrets.get("access_key_id", "")
        self.secret_access_key = secrets.get("secret_access_key", "")
        self.session_token = secrets.get("session_token")
        self.client = env.http(timeout=httpx.Timeout(config.timeout_seconds))
        if config.base_url:
            parsed = urlsplit(config.base_url.rstrip("/"))
            self._scheme = parsed.scheme or "https"
            self._endpoint_host = parsed.netloc
            self._base_path = parsed.path.rstrip("/")
        else:
            self._scheme = "https"
            self._endpoint_host = None
            self._base_path = ""

    def _require_keys(self) -> None:
        if not self.access_key_id or not self.secret_access_key:
            raise ExecutionError(
                "secret_missing",
                f"{self.name}: S3 requires access_key_id and secret_access_key.",
                retryable=False,
                provider=self.name,
            )

    def _signed(self, method: str, host: str, path: str, query: list[tuple[str, str]]) -> dict[str, str]:
        self._require_keys()
        return sign_headers(
            method=method,
            host=host,
            path=path,
            query=query,
            region=self.config.region,
            access_key=self.access_key_id,
            secret_key=self.secret_access_key,
            session_token=self.session_token,
            payload_hash=EMPTY_SHA256,
        )

    def _root(self) -> tuple[str, str, str]:
        if self._endpoint_host:
            return self._scheme, self._endpoint_host, self._base_path or "/"
        return "https", f"s3.{self.config.region}.amazonaws.com", "/"

    def _locate(self, bucket: str, key: str | None) -> tuple[str, str, str]:
        """(scheme, host, canonical path) for a bucket, and optionally an object key."""
        if self._endpoint_host:
            scheme = self._scheme
            host = self._endpoint_host
            base = self._base_path
            if self.config.path_style:
                path = f"{base}/{bucket}"
            else:
                host = f"{bucket}.{host}"
                path = base
        else:
            scheme = "https"
            host = f"{bucket}.s3.{self.config.region}.amazonaws.com"
            path = ""
        if key:
            encoded = quote(key, safe="/-_.~")
            path = f"{path}/{encoded}" if path else f"/{encoded}"
        else:
            path = f"{path}/" if path else "/"
        return scheme, host, path

    def _request(self, method: str, scheme: str, host: str, path: str, query: list[tuple[str, str]]) -> httpx.Response:
        headers = self._signed(method, host, path, query)
        qs = _canonical_query(query)
        url = f"{scheme}://{host}{path}" + (f"?{qs}" if qs else "")
        try:
            resp = self.client.request(method, url, headers=headers)
        except httpx.HTTPError as exc:
            raise classify_exception(exc, self.name, url) from exc
        error = classify_response(resp, self.name)
        if error is not None:
            raise error
        return resp

    def check(self) -> None:
        # ListBuckets: proves the endpoint is reachable and the credentials are accepted.
        scheme, host, path = self._root()
        self._request("GET", scheme, host, path, [])

    def scan(self, scope: Mapping[str, Any], cursor: str | None) -> ScanResult:
        bucket = scope.get("bucket")
        if not isinstance(bucket, str) or not bucket:
            raise ExecutionError("invalid_scope", "S3 scope requires a string 'bucket'.", retryable=False, provider=self.name)
        prefix = scope.get("prefix") or ""
        scheme, host, path = self._locate(bucket, None)
        query: list[tuple[str, str]] = [("list-type", "2")]
        if prefix:
            query.append(("prefix", str(prefix)))
        if cursor:
            query.append(("continuation-token", cursor))
        resp = self._request("GET", scheme, host, path, query)
        root = ET.fromstring(resp.content)
        items = []
        for node in root:
            if _local(node.tag) != "Contents":
                continue
            key = _child_text(node, "Key")
            if key is None:
                continue
            size = _child_text(node, "Size")
            items.append(
                RemoteItem(
                    remote_id=key,
                    name=key.rsplit("/", 1)[-1] or key,
                    mime_type=None,
                    size=int(size) if size is not None and size.isdigit() else None,
                    version=_child_text(node, "ETag"),
                    modified_at=_child_text(node, "LastModified"),
                    deleted=False,
                    path=key,
                )
            )
        return ScanResult(items=iter(items), next_cursor=_child_text(root, "NextContinuationToken"), full=True)

    def fetch(self, scope: Mapping[str, Any], item: RemoteItem) -> FetchedFile:
        bucket = scope.get("bucket")
        if not isinstance(bucket, str) or not bucket:
            raise ExecutionError("invalid_scope", "S3 scope requires a string 'bucket'.", retryable=False, provider=self.name)
        scheme, host, path = self._locate(bucket, item.remote_id)
        resp = self._request("GET", scheme, host, path, [])
        mime = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not mime or mime == "application/octet-stream":
            mime = mimetypes.guess_type(item.name)[0] or "application/octet-stream"
        version = resp.headers.get("etag")
        # Source identity goes in attributes (free-form), never metadata: metadata is
        # schema-validated, and the connector cannot know which fields the corpus declares.
        attributes = {
            "s3_key": item.remote_id,
            "s3_etag": version or "",
            "s3_last_modified": item.modified_at or "",
            "path": item.path,
            "source_url": f"s3://{bucket}/{item.remote_id}",
        }
        return FetchedFile(
            content=resp.content,
            filename=item.name,
            mime_type=mime,
            version=version,
            metadata={},
            attributes=attributes,
        )

    def close(self) -> None:
        self.client.close()


def _factory(config: Any, secrets: dict[str, str], env: BuildEnv) -> S3Connector:
    assert isinstance(config, S3Config)
    return S3Connector(config, secrets, env)


SPEC = ProviderTypeSpec(
    kind="connector",
    type="s3",
    label="S3 / S3-compatible object storage",
    description="List and fetch objects from an S3 bucket (or MinIO/R2/GCS via S3 interop).",
    config_model=S3Config,
    fingerprint_fields=("base_url", "region", "path_style"),
    secret_keys=("access_key_id", "secret_access_key", "session_token"),
    factory=_factory,
)
