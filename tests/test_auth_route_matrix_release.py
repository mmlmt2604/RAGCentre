"""An independent route permission oracle, with denied-request side-effect checks."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.accounts import SESSION_COOKIE
from ragcentre.api.app import create_app
from ragcentre.context import AppContext, open_context
from ragcentre.settings import Settings

# Explicit policy inventory. Never derive expected access from RequestGuard's allowlist.
PUBLIC = {
    ("GET", "/v1/health"),
    ("GET", "/v1/auth/config"),
    ("GET", "/v1/auth/whoami"),
    ("POST", "/v1/auth/setup"),
    ("POST", "/v1/auth/login"),
}
READ = {
    ("GET", "/v1/corpora"),
    ("GET", "/v1/corpora/{corpus_id}"),
    ("GET", "/v1/corpora/{corpus_id}/documents"),
    ("GET", "/v1/corpora/{corpus_id}/pipelines"),
    ("GET", "/v1/documents/{document_id}"),
    ("GET", "/v1/documents/{document_id}/chunks"),
    ("GET", "/v1/documents/{document_id}/content"),
    ("GET", "/v1/documents/{document_id}/parsed"),
    ("GET", "/v1/documents/{document_id}/outline"),
    ("GET", "/v1/chunks/{chunk_id}"),
    ("GET", "/v1/pipelines/{pipeline_id}/status"),
    ("POST", "/v1/corpora/{corpus_id}/query"),
}
SESSION_ONLY = {
    ("POST", "/v1/auth/logout"),
    ("POST", "/v1/auth/password"),
    ("GET", "/v1/auth/me"),
    ("GET", "/v1/auth/csrf"),
    ("GET", "/v1/keys"),
    ("POST", "/v1/keys"),
    ("DELETE", "/v1/keys/{key_id}"),
}
FULL_ONLY = {
    ("POST", "/v1/admin/gc"),
    ("POST", "/v1/admin/cache/clear"),
    ("GET", "/v1/provider-types"),
    ("GET", "/v1/providers"),
    ("POST", "/v1/providers"),
    ("GET", "/v1/providers/{provider_id}"),
    ("PATCH", "/v1/providers/{provider_id}"),
    ("DELETE", "/v1/providers/{provider_id}"),
    ("POST", "/v1/providers/{provider_id}/test"),
    ("PUT", "/v1/providers/{provider_id}/secrets/{key}"),
    ("DELETE", "/v1/providers/{provider_id}/secrets/{key}"),
    ("GET", "/v1/ocr-services"),
    ("POST", "/v1/ocr-services/scan"),
    ("POST", "/v1/providers/{provider_id}/ocr-check"),
    ("POST", "/v1/corpora"),
    ("PATCH", "/v1/corpora/{corpus_id}/metadata-schema"),
    ("PATCH", "/v1/corpora/{corpus_id}"),
    ("PUT", "/v1/corpora/{corpus_id}/active-pipeline"),
    ("POST", "/v1/corpora/{corpus_id}/pipelines"),
    ("GET", "/v1/pipelines/{pipeline_id}"),
    ("GET", "/v1/pipelines/{pipeline_id}/diff"),
    ("POST", "/v1/pipelines/{pipeline_id}/retry-failed"),
    ("POST", "/v1/pipelines/{pipeline_id}/archive"),
    ("POST", "/v1/corpora/{corpus_id}/documents"),
    ("PATCH", "/v1/documents/{document_id}"),
    ("DELETE", "/v1/documents/{document_id}"),
    ("PUT", "/v1/documents/{document_id}/content"),
    ("GET", "/v1/node-runs"),
    ("POST", "/v1/node-runs/{run_id}/retry"),
    ("POST", "/v1/corpora/{corpus_id}/eval/datasets"),
    ("GET", "/v1/corpora/{corpus_id}/eval/datasets"),
    ("GET", "/v1/eval/datasets/{dataset_id}"),
    ("DELETE", "/v1/eval/datasets/{dataset_id}"),
    ("GET", "/v1/eval/datasets/{dataset_id}/items"),
    ("GET", "/v1/corpora/{corpus_id}/eval/runs"),
    ("POST", "/v1/eval/runs"),
    ("GET", "/v1/eval/runs/{run_id}"),
    ("GET", "/v1/eval/runs/{run_id}/items"),
}
DOCS = {
    (method, path)
    for method in ("GET", "HEAD")
    for path in ("/v1/docs", "/v1/docs/oauth2-redirect", "/v1/openapi.json")
}
PROTECTED = READ | SESSION_ONLY | FULL_ONLY | DOCS


@pytest.fixture(scope="module")
def guarded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[AppContext, dict[str, str]]]:
    ctx = open_context(
        Settings(
            data_dir=tmp_path_factory.mktemp("auth-route-matrix"),
            workers=0,
            discovery_urls="off",
            api_token="release-env-token",
        )
    )
    try:
        user = ctx.accounts.create_account("release-matrix", "", "release-matrix-password")
        session, _ = ctx.accounts.create_session(user["id"], expected_auth_version=user["auth_version"])
        read = ctx.accounts.create_key("matrix-read", "read")["key"]
        full = ctx.accounts.create_key("matrix-full", "full")["key"]
        yield ctx, {"session": session, "read": read, "full": full, "legacy": "release-env-token"}
    finally:
        ctx.close()


def snapshot(ctx: AppContext) -> dict[str, list[tuple[Any, ...]]]:
    # Key usage timestamps are allowed bookkeeping; business-resource writes are not.
    tables = (
        "users",
        "local_credentials",
        "user_sessions",
        "corpora",
        "documents",
        "pipelines",
        "providers",
        "node_runs",
    )
    result = {table: [tuple(row) for row in ctx.db.all(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}
    result["api_keys"] = [
        tuple(row)
        for row in ctx.db.all(
            "SELECT id, name, scope, token_hash, team_id, created_by_user_id, created_at FROM api_keys ORDER BY rowid"
        )
    ]
    return result


def test_every_openapi_operation_is_explicitly_classified() -> None:
    operations = {
        (method.upper(), path)
        for path, item in create_app(None).openapi()["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete", "head", "options"}
    }
    assert operations == PUBLIC | READ | SESSION_ONLY | FULL_ONLY


@pytest.mark.parametrize(
    ("method", "path", "credential"),
    [
        (method, path, credential)
        for method, path in sorted(PROTECTED)
        for credential in ("anonymous", "invalid", "read", "read_with_session")
        if not (credential.startswith("read") and (method, path) in READ)
    ],
)
def test_route_permission_denials(
    guarded: tuple[AppContext, dict[str, str]], method: str, path: str, credential: str
) -> None:
    ctx, secrets = guarded
    before = snapshot(ctx)
    headers = {}
    if credential == "invalid":
        headers = {"Authorization": "Bearer invalid-release-key"}
    elif credential.startswith("read"):
        headers = {"Authorization": f"Bearer {secrets['read']}"}
    if credential == "read_with_session":
        headers["Cookie"] = f"{SESSION_COOKIE}={secrets['session']}"
    concrete = re.sub(r"\{[^}]+\}", "missing-resource", path)
    with TestClient(create_app(ctx)) as client:
        response = client.request(method, concrete, headers=headers, json={"name": "must-not-exist"})
    expected = 403 if credential.startswith("read") else 401
    assert response.status_code == expected, (credential, method, path, response.status_code)
    if method != "HEAD":
        assert response.json()["error"]["code"] == ("read_only_token" if expected == 403 else "unauthorized")
    assert snapshot(ctx) == before, "Denied requests must not mutate business resources"


@pytest.mark.parametrize(("method", "path"), sorted(SESSION_ONLY))
@pytest.mark.parametrize("credential", ["full", "legacy"])
def test_bearer_cannot_manage_browser_credentials(
    guarded: tuple[AppContext, dict[str, str]], method: str, path: str, credential: str
) -> None:
    ctx, secrets = guarded
    before = snapshot(ctx)
    with TestClient(create_app(ctx)) as client:
        response = client.request(
            method,
            re.sub(r"\{[^}]+\}", "missing-resource", path),
            headers={"Authorization": f"Bearer {secrets[credential]}"},
            json={},
        )
    assert response.status_code == 401
    assert snapshot(ctx) == before


def test_invalid_bearer_never_falls_back_to_session(guarded: tuple[AppContext, dict[str, str]]) -> None:
    ctx, secrets = guarded
    with TestClient(create_app(ctx)) as client:
        response = client.get(
            "/v1/corpora",
            headers={"Authorization": "Bearer invalid", "Cookie": f"{SESSION_COOKIE}={secrets['session']}"},
        )
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/v1/documents/{document_id}/outline", "/v1/chunks/{chunk_id}"])
def test_mcp_read_contract_endpoints_are_registered(path: str) -> None:
    assert "get" in create_app(None).openapi()["paths"].get(path, {}), (
        "Advertised MCP fetch endpoints must be implemented"
    )
