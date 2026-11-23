from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from ragcentre.api.app import create_app
from ragcentre.context import open_context
from ragcentre.db.migrate import current_version, latest_version, split_statements
from ragcentre.settings import Settings


def test_health_reports_migration(client: TestClient) -> None:
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "status": "ok",
        "version": body["version"],
        "migration": latest_version(),
        "workers": 0,
        "auth_required": False,
        "secret_storage_enabled": False,
    }
    assert latest_version() >= 1


def test_starting_twice_does_not_rerun_migrations(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    first = open_context(settings)
    try:
        with first.db.connection() as conn:
            assert current_version(conn) == latest_version()
        first.db.execute(
            "INSERT INTO corpora (id, name, metadata_schema, created_at) VALUES ('c1', 'keep', '{}', 'now')"
        )
    finally:
        first.close()
    from ragcentre.db.migrate import migrate

    second = open_context(settings)
    try:
        assert migrate(second.db) == []
        assert second.db.scalar("SELECT name FROM corpora WHERE id = 'c1'") == "keep"
    finally:
        second.close()


def test_settings_from_env() -> None:
    s = Settings.from_env({"RAGCENTRE_DATA_DIR": "/x", "RAGCENTRE_PORT": "9000", "RAGCENTRE_API_TOKEN": "t"})
    assert s.data_dir == Path("/x") and s.port == 9000 and s.api_token == "t"
    assert Settings.from_env({}).workers == 4
    assert Settings.from_env({}).max_upload_mb == 200


def test_split_statements_handles_comments() -> None:
    sql = "-- a comment\nCREATE TABLE a (x TEXT); -- trailing\n\nCREATE TABLE b (y TEXT DEFAULT ';');\n"
    assert len(split_statements(sql)) == 2


def test_bearer_token(tmp_path: Path) -> None:
    ctx = open_context(Settings(data_dir=tmp_path, api_token="s3cret"))
    try:
        with TestClient(create_app(ctx)) as c:
            assert c.get("/v1/health").status_code == 200
            assert c.get("/v1/health").json()["auth_required"] is True
            r = c.get("/v1/nope")
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "unauthorized"
            r = c.get("/v1/nope", headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401
            r = c.get("/v1/nope", headers={"Authorization": "Bearer s3cret"})
            assert r.status_code == 404
            assert r.json()["error"]["code"] == "not_found"
    finally:
        ctx.close()


def test_unknown_route_uses_error_shape(client: TestClient) -> None:
    r = client.get("/v1/does-not-exist")
    assert r.status_code == 404
    assert set(r.json()["error"]) >= {"code", "message"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_serve_starts_and_answers_health(tmp_path: Path) -> None:
    port = _free_port()
    env = dict(os.environ, RAGCENTRE_DATA_DIR=str(tmp_path), RAGCENTRE_WORKERS="1", RAGCENTRE_LOG_LEVEL="WARNING")
    for attempt in range(2):  # the second start must not re-run migrations
        proc = subprocess.Popen(
            [sys.executable, "-m", "ragcentre.cli", "serve", "--port", str(port)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 20
            body = None
            while time.monotonic() < deadline:
                try:
                    r = httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=1)
                    body = r.json()
                    break
                except httpx.HTTPError:
                    if proc.poll() is not None:
                        raise AssertionError(proc.stderr.read().decode() if proc.stderr else "exited") from None
                    time.sleep(0.2)
            assert body is not None, f"server did not start (attempt {attempt})"
            assert body["migration"] == latest_version()
        finally:
            proc.terminate()
            proc.wait(timeout=15)
