from __future__ import annotations

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


def _team(client: TestClient, name: str) -> dict[str, Any]:
    r = client.post("/v1/teams", json={"name": name}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _token(client: TestClient, team_id: str, kind: str, name: str) -> str:
    r = client.post(f"/v1/teams/{team_id}/tokens", json={"kind": kind, "name": name}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _corpus(client: TestClient, name: str, team_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if team_id is not None:
        body["team_id"] = team_id
    r = client.post("/v1/corpora", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _connector(client: TestClient, name: str, team_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "kind": "connector", "type": "s3", "config": {}}
    if team_id is not None:
        body["team_id"] = team_id
    r = client.post("/v1/providers", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _source(client: TestClient, corpus_id: str, name: str, provider_id: str) -> dict[str, Any]:
    r = client.post(
        f"/v1/corpora/{corpus_id}/sources",
        json={"name": name, "provider_id": provider_id, "scope": {"bucket": "b"}, "schedule_seconds": 0},
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_source_crud(world: tuple[AppContext, TestClient]) -> None:
    _, client = world
    corpus = _corpus(client, "c")
    provider = _connector(client, "s3")
    src = _source(client, corpus["id"], "s", provider["id"])

    assert src["status"] == "idle"
    assert src["next_run_at"] is None  # schedule 0 = manual only

    got = client.get(f"/v1/sources/{src['id']}", headers=ADMIN)
    assert got.status_code == 200
    assert got.json()["scope"] == {"bucket": "b"}

    patched = client.patch(
        f"/v1/sources/{src['id']}", json={"scope": {"bucket": "b2"}, "schedule_seconds": 60}, headers=ADMIN
    )
    assert patched.status_code == 200
    assert patched.json()["scope"] == {"bucket": "b2"}
    assert patched.json()["schedule_seconds"] == 60

    listed = client.get(f"/v1/corpora/{corpus['id']}/sources", headers=ADMIN)
    assert [s["id"] for s in listed.json()["items"]] == [src["id"]]

    items = client.get(f"/v1/sources/{src['id']}/items", headers=ADMIN)
    assert items.status_code == 200
    assert items.json()["items"] == []

    assert client.delete(f"/v1/sources/{src['id']}", headers=ADMIN).status_code == 204
    assert client.get(f"/v1/sources/{src['id']}", headers=ADMIN).status_code == 404


def test_sync_enqueues_exactly_one_task(world: tuple[AppContext, TestClient]) -> None:
    ctx, client = world
    corpus = _corpus(client, "c")
    provider = _connector(client, "s3")
    src = _source(client, corpus["id"], "s", provider["id"])

    assert client.post(f"/v1/sources/{src['id']}/sync", headers=ADMIN).status_code == 200
    assert ctx.db.scalar("SELECT status FROM sync_sources WHERE id = ?", (src["id"],)) == "syncing"
    assert ctx.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'sync_source'") == 1

    # A second sync while one is in flight must not enqueue another task.
    assert client.post(f"/v1/sources/{src['id']}/sync", headers=ADMIN).status_code == 200
    assert ctx.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'sync_source'") == 1


def test_create_source_requires_connector_provider(world: tuple[AppContext, TestClient]) -> None:
    _, client = world
    corpus = _corpus(client, "c")
    r = client.post(
        "/v1/providers",
        json={"name": "emb", "kind": "embedder", "type": "builtin_hash", "config": {}},
        headers=ADMIN,
    )
    embedder = r.json()
    r = client.post(
        f"/v1/corpora/{corpus['id']}/sources",
        json={"name": "s", "provider_id": embedder["id"], "scope": {}},
        headers=ADMIN,
    )
    assert r.status_code == 422, r.text


def test_duplicate_source_name_is_conflict(world: tuple[AppContext, TestClient]) -> None:
    _, client = world
    corpus = _corpus(client, "c")
    provider = _connector(client, "s3")
    _source(client, corpus["id"], "s", provider["id"])
    r = client.post(
        f"/v1/corpora/{corpus['id']}/sources",
        json={"name": "s", "provider_id": provider["id"], "scope": {}},
        headers=ADMIN,
    )
    assert r.status_code == 409, r.text


def test_cross_team_source_and_provider_are_404(world: tuple[AppContext, TestClient]) -> None:
    _, client = world
    team_a = _team(client, "alpha")
    team_b = _team(client, "beta")
    token_b = _token(client, team_b["id"], "full", "b-token")

    corpus_a = _corpus(client, "a-corpus", team_a["id"])
    provider_a = _connector(client, "a-s3", team_a["id"])
    src_a = _source(client, corpus_a["id"], "a-src", provider_a["id"])

    # Team B cannot see A's source, its provider, or create a source with A's provider.
    assert client.get(f"/v1/sources/{src_a['id']}", headers=_auth(token_b)).status_code == 404
    assert client.post(f"/v1/sources/{src_a['id']}/sync", headers=_auth(token_b)).status_code == 404

    corpus_b = _corpus(client, "b-corpus", team_b["id"])
    r = client.post(
        f"/v1/corpora/{corpus_b['id']}/sources",
        json={"name": "s", "provider_id": provider_a["id"], "scope": {}},
        headers=_auth(token_b),
    )
    assert r.status_code == 404, r.text

    # A read token is rejected on every mutating source route.
    token_read = _token(client, team_b["id"], "read", "b-read")
    assert client.post(f"/v1/sources/{src_a['id']}/sync", headers=_auth(token_read)).status_code in (403, 404)
