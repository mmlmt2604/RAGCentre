"""Strict origin matching with positive controls and real proxy middleware."""

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from ragcentre.api.app import create_app
from ragcentre.api.principal import csrf_valid
from ragcentre.context import AppContext


@pytest.mark.parametrize(
    ("host", "scheme", "origin", "allowed"),
    [
        ("testserver", "http", "http://testserver", True),
        ("TESTSERVER:80", "http", "http://testserver", True),
        ("testserver", "https", "https://testserver:443", True),
        ("[::1]:8700", "http", "http://[0:0:0:0:0:0:0:1]:8700", True),
        ("testserver", "http", "https://testserver", False),
        ("testserver", "http", "http://testserver:9000", False),
        ("testserver", "http", "null", False),
        ("testserver", "http", "", False),
        ("testserver", "http", "http://[", False),
        ("testserver", "http", "http://testserver:", False),
        ("testserver", "http", "http://testserver:65536", False),
        ("testserver", "http", "http://testserver/", False),
        ("testserver", "http", "http://testserver?", False),
        ("testserver", "http", "http://testserver#", False),
        ("testserver", "http", "http://user@testserver", False),
        ("testserver", "http", "http://testserver http://evil.example", False),
        ("testserver", "http", " http://testserver", False),
        ("testserver", "http", "http://test\tserver", False),
        ("testserver", "http", "http://testserver\\evil", False),
        ("[::1]:8700", "http", "http://[::1]junk:8700", False),
        ("testserver", "http", "file://testserver", False),
    ],
)
def test_origin_parser(host: str, scheme: str, origin: str, allowed: bool) -> None:
    headers = Headers({"host": host, "origin": origin, "x-ragcentre-csrf": "secret"})
    assert csrf_valid(headers, {"csrf_secret": "secret"}, scheme=scheme) is allowed


@pytest.mark.parametrize("duplicate", ["origin", "host"])
def test_duplicate_authority_headers_rejected(duplicate: str) -> None:
    headers = Headers(
        raw=[
            (b"host", b"testserver"),
            (b"origin", b"http://testserver"),
            (b"x-ragcentre-csrf", b"secret"),
            (duplicate.encode(), b"http://testserver" if duplicate == "origin" else b"testserver"),
        ]
    )
    assert not csrf_valid(headers, {"csrf_secret": "secret"}, scheme="http")


@pytest.mark.parametrize("supplied", ["secret", "wrong", ""])
def test_missing_origin_still_requires_csrf(supplied: str) -> None:
    assert csrf_valid(Headers({"x-ragcentre-csrf": supplied}), {"csrf_secret": "secret"}, scheme="http") is (
        supplied == "secret"
    )


@pytest.mark.parametrize("origin", ["http://[", "", "null", "http://testserver:bad"])
def test_malformed_origin_is_http_denial_without_side_effects(ctx: AppContext, client: TestClient, origin: str) -> None:
    csrf = client.post("/v1/auth/setup", json={"username": "origin-user", "password": "origin-test-password"}).json()[
        "csrf"
    ]
    before = ctx.db.scalar("SELECT COUNT(*) FROM corpora")
    response = client.post("/v1/corpora", json={"name": "denied"}, headers={"Origin": origin, "X-RAGCentre-CSRF": csrf})
    assert response.status_code == 403
    assert ctx.db.scalar("SELECT COUNT(*) FROM corpora") == before


@pytest.mark.parametrize("trusted", [True, False])
def test_effective_proxy_scheme_controls_origin_and_secure_cookie(ctx: AppContext, trusted: bool) -> None:
    app = ProxyHeadersMiddleware(create_app(ctx), trusted_hosts="*" if trusted else "192.0.2.10")
    with TestClient(app, base_url="http://public.example") as client:
        response = client.post(
            "/v1/auth/setup",
            json={"username": "proxy-user", "password": "origin-test-password"},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert response.status_code == 201
        assert ("Secure" in response.headers["set-cookie"]) is trusted
        # Replay the cookie explicitly to model TLS termination at the proxy; the upstream
        # request is HTTP and forwarding affects its ASGI scheme only for trusted peers.
        response = client.post(
            "http://public.example/v1/corpora",
            json={"name": "proxy-origin"},
            headers={
                "Cookie": response.headers["set-cookie"].split(";", 1)[0],
                "X-RAGCentre-CSRF": response.json()["csrf"],
                "Origin": "https://public.example",
                "X-Forwarded-Proto": "https",
            },
        )
        assert response.status_code == (201 if trusted else 403)


@pytest.mark.parametrize("base", ["http://[::1]:8700", "http://testserver:80", "https://testserver"])
def test_matching_http_origin_accepts_protected_mutation(ctx: AppContext, base: str) -> None:
    with TestClient(create_app(ctx), base_url=base) as client:
        setup = client.post("/v1/auth/setup", json={"username": "origin-user", "password": "origin-test-password"})
        response = client.post(
            "/v1/corpora", json={"name": "allowed"}, headers={"Origin": base, "X-RAGCentre-CSRF": setup.json()["csrf"]}
        )
        assert response.status_code == 201
