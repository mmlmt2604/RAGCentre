"""Provider destination policy for configuration and HTTP socket connections."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from ragcentre.errors import ValidationFailed

_ENDPOINT_FIELDS = (
    "base_url",
    "url",
    "uri",
    "host",
    "cloud_host",
    "controller_host",
    "index_host",
    "connection_string",
)
_METADATA_ADDRESSES = {"100.100.100.200", "168.63.129.16", "fd00:ec2::254"}


def _allowed_address(host: str, address: str, allow: tuple[str, ...]) -> bool:
    ip = ipaddress.ip_address(address.partition("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_link_local or str(ip) in _METADATA_ADDRESSES:
        return False
    if not allow or host.lower().rstrip(".") in allow:
        return True
    for entry in allow:
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue  # The entry is a hostname.
    return False


def _addresses(host: str, port: int, allow: tuple[str, ...]) -> list[str]:
    addresses = list(dict.fromkeys(str(row[4][0]) for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses or not all(_allowed_address(host, address, allow) for address in addresses):
        raise ValueError("Provider destination is blocked by the egress policy.")
    return addresses


def validate_destination(config: Mapping[str, Any], allow: tuple[str, ...]) -> None:
    for field in _ENDPOINT_FIELDS:
        value = config.get(field)
        if not isinstance(value, str) or not value:
            continue
        if field == "uri" and "://" not in value:
            continue  # Local paths are checked separately.
        try:
            parsed = urlsplit(value if "://" in value else f"//{value}")
            # MongoDB connection strings can contain multiple hosts.
            authority = parsed.netloc.rsplit("@", 1)[-1]
            for endpoint in authority.split(","):
                host = urlsplit(f"//{endpoint}").hostname
                if host is None:
                    raise ValueError("Missing destination hostname.")
                try:
                    _addresses(host, 0, allow)
                except socket.gaierror:
                    # Names can become available after configuration. HTTP always checks again on connect.
                    if allow and host.lower().rstrip(".") not in allow:
                        raise ValueError("Unresolved destination is outside the allowlist.") from None
        except ValueError:
            raise ValidationFailed(
                f"Provider configuration field {field} has an invalid or blocked destination."
            ) from None


class CheckedNetworkBackend(httpcore.SyncBackend):
    def __init__(self, allow: tuple[str, ...]) -> None:
        self.allow = allow

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        try:
            addresses = _addresses(host, port, self.allow)
        except (ValueError, OSError):
            raise httpcore.ConnectError("Provider destination is blocked or could not be resolved.") from None
        last_error: Exception | None = None
        for address in addresses:
            try:
                # HTTP Core retains the original hostname for TLS SNI and certificate verification.
                return super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error


class CheckedHTTPTransport(httpx.HTTPTransport):
    def __init__(self, allow: tuple[str, ...]) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=CheckedNetworkBackend(allow),
        )


def checked_http_client(allow: tuple[str, ...] = (), **kwargs: Any) -> httpx.Client:
    # Keep the egress policy even when callers explicitly request these same settings.
    kwargs.update(transport=CheckedHTTPTransport(allow), trust_env=False, follow_redirects=False)
    return httpx.Client(**kwargs)
