"""Browser origin and cookie policy independent of bearer authentication."""

from __future__ import annotations

import hmac
import ipaddress
import re
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.types import Scope

from ragcentre.errors import Forbidden, ValidationFailed
from ragcentre.settings import Settings


def loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def validate_browser_settings(settings: Settings) -> None:
    if len(settings.browser_base_path) > 200 or not re.fullmatch(r"(?:/[a-zA-Z0-9_-]+)*", settings.browser_base_path):
        raise ValidationFailed(
            "RAGCENTRE_BROWSER_BASE_PATH must be empty or a path such as /console, without a trailing slash."
        )
    if settings.insecure_local_cookie and not loopback(settings.host):
        raise ValidationFailed("Insecure session cookies are restricted to loopback development servers.")
    if settings.browser_origin:
        origin = urlsplit(settings.browser_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.netloc
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValidationFailed("RAGCENTRE_BROWSER_ORIGIN must be an exact http(s) origin without a path.")
        if origin.scheme != "https" and not settings.insecure_local_cookie:
            raise ValidationFailed("Local login requires HTTPS, or explicit loopback development cookies.")
        if settings.insecure_local_cookie and not loopback(origin.hostname or ""):
            raise ValidationFailed("The development browser origin must be loopback.")
    elif not loopback(settings.host):
        raise ValidationFailed("Set RAGCENTRE_BROWSER_ORIGIN for a non-loopback local-auth server.")


def cookie_name(settings: Settings) -> str:
    return "ragcentre_session_dev" if settings.insecure_local_cookie else "__Host-ragcentre_session"


def check_origin(settings: Settings, headers: Headers, scope: Scope) -> None:
    origin = headers.get("origin", "")
    if settings.browser_origin:
        expected = settings.browser_origin
    else:
        # Only loopback development may derive an origin; never trust a remote Host header.
        parts = urlsplit(origin)
        if not loopback(settings.host) or not loopback(parts.hostname or ""):
            raise Forbidden("This request requires the configured browser origin.")
        expected = f"{scope.get('scheme', 'http')}://{headers.get('host', '')}"
    if not origin or not hmac.compare_digest(origin, expected):
        raise Forbidden("The browser origin was not accepted.")


def check_csrf(settings: Settings, headers: Headers, scope: Scope, expected: str) -> None:
    check_origin(settings, headers, scope)
    supplied = headers.get("x-ragcentre-csrf", "")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise Forbidden("The session CSRF token was not accepted. Refresh and try again.")
