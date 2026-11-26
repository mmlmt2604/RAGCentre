"""Principal resolution and the read-scope allowlist for the request guard.

The guard authenticates before routing, so these helpers see only the raw ASGI scope and headers.
A principal is a plain dict (``kind``, ``scope`` and any credential-specific fields); handlers read
it through ``get_principal``.
"""

from __future__ import annotations

import hmac
import re
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from starlette.datastructures import Headers

from ragcentre.accounts import SESSION_COOKIE
from ragcentre.context import AppContext

FULL = "full"
READ = "read"
NONE = "none"

# The read-scope allowlist from docs/plans/agent-access.md §6. A read key can list/read corpora,
# documents and pipeline status, and query; everything else is 403 read_only_token. Route templates
# are compiled to regexes because the guard runs before routing and only sees the concrete path.
_READ_ALLOWED: list[tuple[str, re.Pattern[str]]] = [
    ("GET", re.compile(r"^/v1/health$")),
    ("GET", re.compile(r"^/v1/auth/whoami$")),
    ("GET", re.compile(r"^/v1/corpora$")),
    ("GET", re.compile(r"^/v1/corpora/[^/]+$")),
    ("GET", re.compile(r"^/v1/corpora/[^/]+/documents$")),
    ("GET", re.compile(r"^/v1/corpora/[^/]+/pipelines$")),
    ("GET", re.compile(r"^/v1/documents/[^/]+$")),
    ("GET", re.compile(r"^/v1/documents/[^/]+/chunks$")),
    ("GET", re.compile(r"^/v1/documents/[^/]+/content$")),
    ("GET", re.compile(r"^/v1/documents/[^/]+/parsed$")),
    ("GET", re.compile(r"^/v1/documents/[^/]+/outline$")),
    ("GET", re.compile(r"^/v1/chunks/[^/]+$")),
    ("GET", re.compile(r"^/v1/pipelines/[^/]+/status$")),
    ("POST", re.compile(r"^/v1/corpora/[^/]+/query$")),
]


def read_scope_allows(method: str, path: str) -> bool:
    return any(m == method and pattern.match(path) for m, pattern in _READ_ALLOWED)


def _session_token(headers: Headers) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:  # pragma: no cover - malformed cookie header
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel is not None else None


def resolve_principal(ctx: AppContext, headers: Headers) -> dict[str, str]:
    """Resolve a request to a principal. Bearer credentials win over the session cookie, and an
    invalid bearer never falls back to a cookie."""
    api_token = ctx.settings.api_token
    auth = headers.get("authorization", "")
    if auth:
        scheme, _, supplied = auth.partition(" ")
        if scheme.lower() == "bearer" and supplied.strip():
            if api_token is not None and hmac.compare_digest(supplied.strip().encode(), api_token.encode()):
                return {"kind": "admin_token", "scope": FULL}
            key = ctx.accounts.key_for(supplied.strip())
            if key is not None:
                return {"kind": "key", "scope": key["scope"], "key_id": key["id"]}
        # An invalid bearer is anonymous; it must not fall back to a cookie.
        return {"kind": "none", "scope": NONE}
    token = _session_token(headers)
    if token is not None:
        session = ctx.accounts.session_for(token)
        if session is not None:
            return {
                "kind": "session",
                "scope": FULL,
                "user_id": session["user_id"],
                "csrf_secret": session["csrf_secret"],
            }
    return {"kind": "none", "scope": NONE}


def _hostname(value: str) -> str:
    return (urlsplit(value).hostname or "").lower()


def csrf_valid(headers: Headers, principal: dict[str, str]) -> bool:
    """Check the session-bound CSRF header and, when present, the request origin."""
    supplied = headers.get("x-ragcentre-csrf", "")
    secret = principal.get("csrf_secret", "")
    if not supplied or not secret or not hmac.compare_digest(supplied.encode(), secret.encode()):
        return False
    origin = headers.get("origin")
    host = headers.get("host")
    if origin and host:
        origin_host = _hostname(origin)
        host_host = host.rsplit(":", 1)[0].lower()
        if origin_host and host_host and origin_host != host_host:
            return False
    return True
