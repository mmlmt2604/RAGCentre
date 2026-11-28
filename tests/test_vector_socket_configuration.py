from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ragcentre.context import AppContext


def test_redis_unix_socket_configuration_is_local(
    ctx: AppContext, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ctx, "settings", replace(ctx.settings, egress_allow=("192.0.2.0/24",)))
    url = f"unix://{tmp_path / 'redis.sock'}?db=0"
    created = client.post(
        "/v1/providers",
        json={"name": "socket-store", "kind": "vector_store", "type": "redis", "config": {"url": url}},
    )
    assert created.status_code == 201, created.text
    provider = created.json()
    assert provider["config"]["url"] == url
    path = f"/v1/providers/{provider['id']}"
    changed_url = f"unix://{tmp_path / 'another.sock'}?db=1"
    changed = client.patch(path, json={"config": {"url": changed_url}})
    assert changed.status_code == 200, changed.text
    assert client.get(path).json()["config"]["url"] == changed_url
    blocked = client.patch(path, json={"config": {"url": "redis://127.0.0.1:6379/0"}})
    assert blocked.status_code == 422, blocked.text


def test_unix_socket_exception_does_not_apply_to_http_providers(client: TestClient, tmp_path: Path) -> None:
    created = client.post(
        "/v1/providers",
        json={
            "name": "bad-http-socket",
            "kind": "embedder",
            "type": "openai_compatible",
            "config": {"base_url": f"unix://{tmp_path / 'redis.sock'}", "model": "example"},
        },
    )
    assert created.status_code == 422, created.text
