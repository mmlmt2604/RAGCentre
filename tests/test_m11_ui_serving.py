"""M11: the built UI is served at / with a fallback to index.html for client-side routes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ragcentre.api.app import create_app
from ragcentre.api.ui import MISSING_MESSAGE
from ragcentre.context import open_context
from ragcentre.settings import Settings

INDEX = "<!doctype html><title>RAGCentre</title><div id=root></div>"


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ui_dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(INDEX, encoding="utf-8")
    (d / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (d / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return d


def _client(tmp_path: Path, ui_dir: Path | None, token: str | None = None) -> Iterator[TestClient]:
    ctx = open_context(Settings(data_dir=tmp_path / "data", workers=0, api_token=token))
    try:
        with TestClient(create_app(ctx, ui_dir=ui_dir)) as c:
            yield c
    finally:
        ctx.close()


@pytest.fixture
def client(tmp_path: Path, ui_dir: Path) -> Iterator[TestClient]:
    yield from _client(tmp_path, ui_dir)


def test_index_and_deep_links(client: TestClient) -> None:
    for path in ("/", "/corpora/abc/pipelines/compare?a=1&b=2", "/providers/new", "/activity"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.text == INDEX
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-cache"


def test_static_files(client: TestClient) -> None:
    r = client.get("/assets/index-abc123.js")
    assert r.status_code == 200 and r.text == "console.log(1)"
    assert "immutable" in r.headers["cache-control"]
    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.get("/assets/index-old999.js").status_code == 404  # a stale bundle is not HTML
    assert client.head("/corpora/x").status_code == 200


def test_api_paths_never_reach_the_ui(client: TestClient) -> None:
    assert client.get("/v1/health").json()["status"] == "ok"
    for path in ("/v1/nope", "/v1", "/v1/corpora/x/nothing"):
        r = client.get(path)
        assert r.status_code == 404, path
        assert r.json()["error"]["code"] == "not_found"


def test_traversal_is_refused(client: TestClient, ui_dir: Path) -> None:
    (ui_dir.parent / "secret.txt").write_text("secret", encoding="utf-8")
    r = client.get("/../secret.txt")
    assert "secret" not in r.text


def test_ui_is_not_behind_the_token_but_the_api_is(tmp_path: Path, ui_dir: Path) -> None:
    for c in _client(tmp_path, ui_dir, token="t0k3n"):
        assert c.get("/").text == INDEX
        assert c.get("/corpora/abc/query").text == INDEX
        assert c.get("/v1/corpora").status_code == 401
        assert c.get("/v1/nope").status_code == 401
        assert c.get("/v1/corpora", headers={"Authorization": "Bearer t0k3n"}).status_code == 200


def test_without_a_build(tmp_path: Path) -> None:
    for c in _client(tmp_path, tmp_path / "missing"):
        r = c.get("/")
        assert r.status_code == 200 and r.text == MISSING_MESSAGE
        assert "npm run build" in r.text
        assert c.get("/corpora/x").status_code == 404
        assert c.get("/v1/health").status_code == 200
