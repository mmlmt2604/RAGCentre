from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ragcentre.api.app import create_app
from ragcentre.context import AppContext, open_context
from ragcentre.settings import Settings

ADMIN = {"Authorization": "Bearer admin-token"}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(tmp_path: Path):
    ctx = open_context(Settings(data_dir=tmp_path / "data", api_token="admin-token", workers=0))
    client = TestClient(create_app(ctx))
    try:
        yield ctx, client
    finally:
        client.close()
        ctx.close()


def _create_team(client: TestClient, name: str, slug: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if slug is not None:
        body["slug"] = slug
    r = client.post("/v1/teams", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _token(client: TestClient, team_id: str, kind: str, name: str) -> dict[str, Any]:
    r = client.post(f"/v1/teams/{team_id}/tokens", json={"kind": kind, "name": name}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _corpus(client: TestClient, name: str, team_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if team_id is not None:
        body["team_id"] = team_id
    r = client.post("/v1/corpora", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


# ---- migration and bootstrap -------------------------------------------------------------------


def test_default_team_and_builtin_scope(world: tuple[AppContext, TestClient]) -> None:
    ctx, _ = world
    assert ctx.migration == 6
    assert ctx.db.scalar("SELECT id FROM teams WHERE slug = 'default'") == "00000000-0000-0000-0000-000000000001"
    assert ctx.db.scalar("SELECT team_id FROM providers WHERE name = 'builtin-text'") is None


def test_corpora_team_id_is_not_null(world: tuple[AppContext, TestClient]) -> None:
    ctx, _ = world
    # The migration adds team_id NOT NULL DEFAULT; a row inserted without it lands in the default team.
    ctx.db.execute("INSERT INTO corpora (id, name, metadata_schema, created_at) VALUES ('c', 'c', '{}', 'now')")
    assert ctx.db.scalar("SELECT team_id FROM corpora WHERE id = 'c'") == "00000000-0000-0000-0000-000000000001"


def test_upgrade_backfills_providers_into_default_team(tmp_path: Path) -> None:
    from ragcentre.db.connection import Database
    from ragcentre.db.migrate import available_migrations, migrate

    db = Database(tmp_path / "upgrade.db")
    try:
        with db.connection() as conn:
            for migration in available_migrations()[:4]:
                conn.executescript(migration.sql)
            conn.execute("PRAGMA user_version = 4")
            # A pre-upgrade user provider and the built-in, both under the 0.0.4 schema (no team_id).
            conn.execute(
                "INSERT INTO providers (id, name, kind, type, config, fingerprint, created_at, updated_at) "
                "VALUES ('p1', 'my-embed', 'embedder', 'openai_compatible', '{}', 'fp1', 'now', 'now')"
            )
            conn.execute(
                "INSERT INTO providers (id, name, kind, type, config, fingerprint, created_at, updated_at) "
                "VALUES ('p2', 'builtin-text', 'parser', 'builtin_text', '{}', 'fp2', 'now', 'now')"
            )
        assert migrate(db) == [5, 6]
        # User providers land in the default team; the built-in stays NULL (system/built-in).
        assert db.scalar("SELECT team_id FROM providers WHERE name = 'my-embed'") == (
            "00000000-0000-0000-0000-000000000001"
        )
        assert db.scalar("SELECT team_id FROM providers WHERE name = 'builtin-text'") is None
    finally:
        db.close()


# ---- teams CRUD --------------------------------------------------------------------------------


def test_teams_are_admin_only(world: tuple[AppContext, TestClient]) -> None:
    _, client = world
    assert client.post("/v1/teams", json={"name": "Alpha"}).status_code == 401
    team = _create_team(client, "Alpha", "alpha")
    assert team["slug"] == "alpha"
    assert client.post("/v1/teams", json={"name": "Alpha Two", "slug": "alpha"}, headers=ADMIN).status_code == 409
    assert client.post("/v1/teams", json={"name": "X", "slug": "UPPER"}, headers=ADMIN).status_code == 422
    assert client.get("/v1/teams", headers=ADMIN).status_code == 200
    # A team token cannot create or list teams.
    tok = _token(client, team["id"], "full", "ci")
    assert client.post("/v1/teams", json={"name": "Nope"}, headers=_auth(tok["token"])).status_code == 403
    assert client.get("/v1/teams", headers=_auth(tok["token"])).status_code == 403


# ---- tokens and whoami -------------------------------------------------------------------------


def test_team_tokens_and_whoami(world: tuple[AppContext, TestClient]) -> None:
    ctx, client = world
    team = _create_team(client, "Alpha", "alpha")
    created = _token(client, team["id"], "read", "reader")
    assert created["token"].startswith("rct_")
    # The raw token is returned once; the stored row is a hash, never the value.
    rows = ctx.db.all("SELECT token_hash FROM team_tokens WHERE team_id = ?", (team["id"],))
    assert created["token"] not in {r["token_hash"] for r in rows}

    assert client.get("/v1/auth/whoami", headers=ADMIN).json() == {
        "scope": "admin",
        "team_id": None,
        "team_name": None,
    }
    assert client.get("/v1/auth/whoami", headers=_auth(created["token"])).json() == {
        "scope": "read",
        "team_id": team["id"],
        "team_name": "Alpha",
    }

    listed = client.get(f"/v1/teams/{team['id']}/tokens", headers=ADMIN).json()["items"]
    assert len(listed) == 1 and "token" not in listed[0]

    r = client.delete(f"/v1/teams/{team['id']}/tokens/{created['id']}", headers=ADMIN)
    assert r.status_code == 204
    assert client.get("/v1/auth/whoami", headers=_auth(created["token"])).status_code == 401


# ---- cross-team isolation ----------------------------------------------------------------------


def test_cross_team_ids_are_not_found(world: tuple[AppContext, TestClient]) -> None:
    _, client = world
    a = _create_team(client, "Alpha", "alpha")
    b = _create_team(client, "Beta", "beta")
    a_tok = _token(client, a["id"], "full", "a-full")["token"]
    b_tok = _token(client, b["id"], "full", "b-full")["token"]

    corpus_a = _corpus(client, "a-docs", a["id"])
    up = client.post(
        f"/v1/corpora/{corpus_a['id']}/documents",
        files={"file": ("a.md", b"# A\n\nalpha text", "text/markdown")},
        headers=_auth(a_tok),
    )
    assert up.status_code == 201, up.text
    doc_id = up.json()["id"]

    # B cannot see A's corpus or document; it looks like a plain 404.
    assert client.get(f"/v1/corpora/{corpus_a['id']}", headers=_auth(b_tok)).status_code == 404
    assert client.get(f"/v1/documents/{doc_id}", headers=_auth(b_tok)).status_code == 404
    assert (
        client.post(
            f"/v1/corpora/{corpus_a['id']}/query",
            json={"query": "alpha"},
            headers=_auth(b_tok),
        ).status_code
        == 404
    )
    # A can see its own.
    assert client.get(f"/v1/corpora/{corpus_a['id']}", headers=_auth(a_tok)).status_code == 200
    assert client.get(f"/v1/documents/{doc_id}", headers=_auth(a_tok)).status_code == 200

    # List endpoints are filtered by team.
    assert [c["name"] for c in client.get("/v1/corpora", headers=_auth(b_tok)).json()["items"]] == []
    assert [c["name"] for c in client.get("/v1/corpora", headers=_auth(a_tok)).json()["items"]] == ["a-docs"]


def test_read_token_can_query_but_not_write(world: tuple[AppContext, TestClient]) -> None:
    _, client = world
    team = _create_team(client, "Alpha", "alpha")
    full = _token(client, team["id"], "full", "full")["token"]
    read = _token(client, team["id"], "read", "read")["token"]
    corpus = _corpus(client, "docs", team["id"])
    client.post(
        f"/v1/corpora/{corpus['id']}/documents",
        files={"file": ("a.md", b"# A\n\ntext", "text/markdown")},
        headers=_auth(full),
    )
    assert client.get(f"/v1/corpora/{corpus['id']}", headers=_auth(read)).status_code == 200
    assert client.post(f"/v1/corpora/{corpus['id']}/documents", headers=_auth(read)).status_code == 403
    assert client.get("/v1/providers", headers=_auth(read)).status_code == 403  # not in the read allowlist
    assert client.get(f"/v1/corpora/{corpus['id']}/documents", headers=_auth(read)).status_code == 403


# ---- provider isolation ------------------------------------------------------------------------


def test_provider_isolation(world: tuple[AppContext, TestClient]) -> None:
    ctx, client = world
    a = _create_team(client, "Alpha", "alpha")
    b = _create_team(client, "Beta", "beta")
    r = client.post(
        "/v1/providers",
        json={"name": "a-embed", "kind": "embedder", "type": "builtin_hash", "config": {}, "team_id": a["id"]},
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text

    assert ctx.providers.by_name_in_team("a-embed", a["id"]) is not None
    assert ctx.providers.by_name_in_team("a-embed", b["id"]) is None
    assert [p.name for p in ctx.providers.list_visible(b["id"])] == ["builtin-text"]
    assert ctx.providers.by_name_in_team("builtin-text", b["id"]) is not None

    # A team token cannot manage the built-in provider.
    tok = _token(client, b["id"], "full", "b-full")["token"]
    builtin = ctx.providers.by_name("builtin-text")
    assert client.patch(f"/v1/providers/{builtin.id}", json={"config": {}}, headers=_auth(tok)).status_code == 403


def test_pipeline_cannot_reference_other_teams_provider(world: tuple[AppContext, TestClient]) -> None:
    ctx, client = world
    a = _create_team(client, "Alpha", "alpha")
    b = _create_team(client, "Beta", "beta")
    client.post(
        "/v1/providers",
        json={"name": "a-embed", "kind": "embedder", "type": "builtin_hash", "config": {}, "team_id": a["id"]},
        headers=ADMIN,
    )
    corpus_b = _corpus(client, "b-docs", b["id"])

    from ragcentre.pipeline.planner import resolve_for_corpus

    cfg = {
        "name": "p",
        "nodes": [
            {"id": "chunk", "type": "chunk", "inputs": [], "strategy": "fixed", "options": {"size": 100}},
            {"id": "embed", "type": "embed", "inputs": ["chunk"], "provider": "a-embed"},
            {
                "id": "vec",
                "type": "index_vector",
                "inputs": ["embed"],
                "provider": "store",
                "options": {"metric": "cosine"},
            },
        ],
        "retrieval": {
            "sources": [{"id": "dense", "index": "vec", "top_k": 10}],
            "top_k": 5,
        },
    }
    res = resolve_for_corpus(ctx, corpus_b["id"], cfg)
    assert not res.valid
    assert any("No provider named" in p.to_json()["message"] for p in res.problems)


# ---- source-grep invariant: id-bearing routes must authorize ------------------------------------


_ROUTER_DIR = Path(__file__).resolve().parent.parent / "src" / "ragcentre" / "api" / "routers"

_REQUIRE_FNS = {
    "require_admin",
    "require_corpus",
    "require_document",
    "require_pipeline",
    "require_dataset",
    "require_eval_run",
    "require_node_run",
    "require_provider",
    "require_provider_owner",
    "require_team",
}

_ID_PARAMS = {"corpus_id", "document_id", "pipeline_id", "dataset_id", "run_id", "provider_id", "team_id", "token_id"}


def test_every_id_route_authorizes() -> None:
    offenders: list[str] = []
    for path in sorted(_ROUTER_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route_paths = [
                dec.args[0].value
                for dec in node.decorator_list
                if isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in {"get", "post", "put", "patch", "delete"}
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
                and isinstance(dec.args[0].value, str)
            ]
            if not route_paths:
                continue
            has_id = any(f"{{{p}}}" in route_paths[0] for p in _ID_PARAMS)
            if not has_id:
                continue
            calls = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _REQUIRE_FNS
            }
            if not calls:
                offenders.append(f"{path.name}:{node.lineno} {route_paths[0]}")
    assert not offenders, "routes with a resource id must call a require_* check: " + ", ".join(offenders)
