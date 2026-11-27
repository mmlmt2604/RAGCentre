"""Real HTTP backend contracts, including actual MCP tools and a stdio process.

Run with: uv run --directory packages/ragcentre-mcp --with-editable ../.. pytest -m contract
The CI job preflights the backend import and rejects zero tests or any skipped contract.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

pytest.importorskip("ragcentre", reason="real-backend contract requires the ragcentre package")

import uvicorn
from ragcentre.api.app import create_app
from ragcentre.context import open_context
from ragcentre.settings import Settings

from ragcentre_mcp.api import ApiError, RagcentreApi
from ragcentre_mcp.server import Options, StartupError, build_server, startup

pytestmark = [pytest.mark.contract, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def wait_ready(client: httpx.Client, pid: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if client.get(f"/pipelines/{pid}/status").json()["status"] == "ready":
            return
        time.sleep(0.02)
    raise AssertionError("Fixture pipeline must become ready")


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    path: Path = tmp_path_factory.mktemp("mcp-backend-contract")
    ctx = open_context(Settings(data_dir=path, workers=0, discovery_urls="off"))
    user = ctx.accounts.create_account("contract-user", "", "contract-release-password")
    read = ctx.accounts.create_key("contract-read", "read")
    full = ctx.accounts.create_key("contract-full", "full")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_app(ctx), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Fixture backend must become ready"
        seeded: list[dict[str, Any]] = []
        with httpx.Client(base_url=f"{url}/v1", headers={"Authorization": f"Bearer {full['key']}"}) as client:
            for name in ("contract-corpus", "hidden-corpus"):
                corpus = client.post("/corpora", json={"name": name})
                assert corpus.status_code == 201
                cid = corpus.json()["id"]
                upload = client.post(
                    f"/corpora/{cid}/documents",
                    files={
                        "file": (
                            "contract.md",
                            ("# Warranty\n\nA lifetime warranty covers this bicycle 自転車 🚲.\n\n" * 40).encode(),
                            "text/markdown",
                        )
                    },
                )
                assert upload.status_code == 201
                did = upload.json()["id"]
                config = {
                    "name": "contract-keyword",
                    "nodes": [
                        {"id": "parse", "type": "parse", "routes": [{"mime": "text/*", "provider": "builtin-text"}]},
                        {
                            "id": "chunk",
                            "type": "chunk",
                            "inputs": ["parse"],
                            "strategy": "fixed",
                            "options": {"max_chars": 500, "overlap_chars": 0},
                        },
                        {"id": "fts", "type": "index_fts", "inputs": ["chunk"]},
                    ],
                    "retrieval": {"sources": [{"id": "keyword", "index": "fts", "top_k": 10}], "top_k": 8},
                }
                pipeline = client.post(f"/corpora/{cid}/pipelines", json=config)
                assert pipeline.status_code == 201
                pid = pipeline.json()["id"]
                seeded.append({"corpus": cid, "document": did, "pipeline": pid, "name": name})
            ctx.start_workers(1)
            for item in seeded:
                wait_ready(client, item["pipeline"])
                assert (
                    client.put(
                        f"/corpora/{item['corpus']}/active-pipeline", json={"pipeline_id": item["pipeline"]}
                    ).status_code
                    == 200
                )
                query = client.post(f"/corpora/{item['corpus']}/query", json={"query": "warranty"})
                assert query.status_code == 200 and query.json()["results"]
                item["chunk"] = query.json()["results"][0]["chunk_id"]
        yield {
            "url": url,
            "read": read["key"],
            "full": full["key"],
            **seeded[0],
            "hidden": seeded[1],
            "ctx": ctx,
            "user": user,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        ctx.close()
        assert not thread.is_alive(), "Fixture backend must stop"


@asynccontextmanager
async def tools(backend: dict[str, Any], *, token: str | None = None, max_chars: int = 12000) -> AsyncIterator[Client]:
    api = RagcentreApi(backend["url"], token or backend["read"])
    options = Options(corpora=(backend["corpus"],), max_chars=max_chars)
    try:
        catalog = await startup(api, options)
        async with Client(build_server(api, catalog, options)) as client:
            yield client
    finally:
        await api.aclose()


async def test_read_key_starts_and_lists_real_corpus(backend: dict[str, Any]) -> None:
    api = RagcentreApi(backend["url"], backend["read"])
    try:
        catalog = await startup(api, Options(corpora=(backend["corpus"],)))
        assert [c.id for c in catalog.corpora] == [backend["corpus"]]
    finally:
        await api.aclose()


async def test_full_key_requires_explicit_override(backend: dict[str, Any]) -> None:
    api = RagcentreApi(backend["url"], backend["full"])
    try:
        with pytest.raises(StartupError, match="--allow-full-token") as error:
            await startup(api, Options())
        assert "RAGCENTRE_READ_TOKEN" not in str(error.value)
        assert "Account" in str(error.value) and "read API key" in str(error.value)
        catalog = await startup(api, Options(allow_full_token=True))
        async with Client(build_server(api, catalog, Options(allow_full_token=True))) as client:
            assert not (await client.call_tool("list_corpora", {})).is_error
    finally:
        await api.aclose()


async def test_real_backend_accepts_mcp_query_payload(backend: dict[str, Any]) -> None:
    api = RagcentreApi(backend["url"], backend["read"])
    try:
        result = await api.query(backend["corpus"], "warranty", top_k=8, filter=None, max_chars=12000)
        assert result.results and result.results[0].document_id == backend["document"]
        assert result.results[0].ordinal is not None
    finally:
        await api.aclose()


async def test_real_backend_supports_document_outline(backend: dict[str, Any]) -> None:
    api = RagcentreApi(backend["url"], backend["read"])
    try:
        outline = await api.outline(backend["document"])
        assert outline.document.id == backend["document"]
        assert outline.chunk_count > 0 and outline.sections
    finally:
        await api.aclose()


async def test_real_backend_supports_chunk_fetch(backend: dict[str, Any]) -> None:
    api = RagcentreApi(backend["url"], backend["read"])
    try:
        chunk = await api.chunk(backend["chunk"], before=1, after=1)
        assert chunk.document.id == backend["document"] and chunk.document.corpus_id == backend["corpus"]
    finally:
        await api.aclose()


async def test_revoked_key_fails_next_backend_read(backend: dict[str, Any]) -> None:
    key = backend["ctx"].accounts.create_key("revocation-contract", "read")
    api = RagcentreApi(backend["url"], key["key"])
    try:
        assert await api.corpora()
        assert backend["ctx"].accounts.revoke_key(key["id"])
        with pytest.raises(ApiError) as error:
            await api.corpora()
        assert error.value.status == 401
    finally:
        await api.aclose()


async def test_stdio_tools_against_real_backend(backend: dict[str, Any]) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ragcentre_mcp", "--corpus", backend["name"]],
        env={**os.environ, "RAGCENTRE_URL": backend["url"], "RAGCENTRE_API_TOKEN": backend["read"]},
    )
    async with Client(params) as client:
        catalog = (await client.list_tools()).tools
        assert {t.name for t in catalog} == {"list_corpora", "search", "fetch"}
        assert all(t.annotations and t.annotations.read_only_hint and t.output_schema for t in catalog)
        listed = await client.call_tool("list_corpora", {})
        assert not listed.is_error
        assert [c["id"] for c in listed.structured_content["corpora"]] == [backend["corpus"]]
        searched = await client.call_tool(
            "search", {"query": "warranty", "filter": {"document_id": backend["document"]}}
        )
        assert not searched.is_error and searched.structured_content["results"]
        hit = searched.structured_content["results"][0]
        fetched = await client.call_tool("fetch", {"id": hit["chunk_id"], "before": 1, "after": 1})
        assert not fetched.is_error and fetched.structured_content["kind"] == "chunk"
        assert any(
            c["chunk_id"] == hit["chunk_id"] and c["role"] == "hit" for c in fetched.structured_content["chunks"]
        )
        outlined = await client.call_tool("fetch", {"id": hit["document_id"]})
        assert not outlined.is_error and outlined.structured_content["kind"] == "document"
        assert outlined.structured_content["chunks"] == [] and outlined.structured_content["sections"]


async def test_real_tool_allowlist_hides_direct_ids(backend: dict[str, Any]) -> None:
    async with tools(backend) as client:
        for id in (backend["hidden"]["chunk"], backend["hidden"]["document"], "missing-id"):
            result = await client.call_tool("fetch", {"id": id})
            assert result.is_error
            text = "\n".join(c.text for c in result.content)
            assert "No chunk or document" in text and "lifetime warranty" not in text
        result = await client.call_tool("search", {"query": "warranty", "corpus": backend["hidden"]["corpus"]})
        assert result.is_error and "No corpus named" in result.content[0].text


async def test_real_tool_output_budget(backend: dict[str, Any]) -> None:
    async with tools(backend, max_chars=200) as client:
        searched = await client.call_tool("search", {"query": "warranty"})
        assert not searched.is_error
        out = searched.structured_content
        assert out["truncated"] and out["results"]
        assert sum(len(r["text"]) for r in out["results"]) <= 200
        fetched = await client.call_tool("fetch", {"id": backend["chunk"], "before": 10, "after": 10})
        assert not fetched.is_error and fetched.structured_content["truncated"]
        assert sum(len(c["text"]) for c in fetched.structured_content["chunks"]) <= 200


async def test_real_tools_fail_after_revocation(backend: dict[str, Any]) -> None:
    key = backend["ctx"].accounts.create_key("tool-revocation-contract", "read")
    async with tools(backend, token=key["key"]) as client:
        assert not (await client.call_tool("search", {"query": "warranty"})).is_error
        assert backend["ctx"].accounts.revoke_key(key["id"])
        for name, args in (
            ("list_corpora", {}),
            ("search", {"query": "warranty"}),
            ("fetch", {"id": backend["chunk"]}),
            ("fetch", {"id": backend["document"]}),
        ):
            result = await client.call_tool(name, args)
            assert result.is_error and "refused" in result.content[0].text.lower()


@pytest.mark.parametrize("token", [None, "invalid-key"])
async def test_invalid_key_cannot_start(backend: dict[str, Any], token: str | None) -> None:
    api = RagcentreApi(backend["url"], token)
    try:
        with pytest.raises(StartupError, match="RAGCENTRE_API_TOKEN") as error:
            await startup(api, Options())
        assert "RAGCENTRE_READ_TOKEN" not in str(error.value)
    finally:
        await api.aclose()


async def test_real_mcp_client_only_issues_reads(backend: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[tuple[str, str]] = []
    original = RagcentreApi._request

    async def record(self: RagcentreApi, method: str, path: str, **kwargs: Any) -> Any:
        requests.append((method, path))
        return await original(self, method, path, **kwargs)

    monkeypatch.setattr(RagcentreApi, "_request", record)
    async with tools(backend) as client:
        for name, args in (
            ("list_corpora", {}),
            ("search", {"query": "warranty"}),
            ("fetch", {"id": backend["chunk"]}),
            ("fetch", {"id": backend["document"]}),
        ):
            assert not (await client.call_tool(name, args)).is_error
    assert requests
    assert all(
        method == "GET" or (method == "POST" and re.fullmatch(r"/corpora/[^/]+/query", path))
        for method, path in requests
    )


async def test_stale_and_deleted_ids_do_not_expose_old_text(backend: dict[str, Any]) -> None:
    # Pause workers so the post-replacement request deterministically sees pending ingestion.
    # These extra documents do not change the stable search/fetch fixture used by other tests.
    from ragcentre.pipeline.executors import execute
    from ragcentre.queue.claims import claim_run, complete

    ctx = backend["ctx"]
    ctx.close_workers()
    try:
        async with httpx.AsyncClient(
            base_url=f"{backend['url']}/v1", headers={"Authorization": f"Bearer {backend['full']}"}
        ) as http:
            r = await http.post(
                f"/corpora/{backend['corpus']}/documents",
                files={"file": ("lifecycle.md", b"old warranty confidential passage", "text/markdown")},
            )
            assert r.status_code == 201
            did = r.json()["id"]
            for _ in range(100):
                run = claim_run(ctx, "contract-drain")
                if run is None:
                    break
                complete(ctx, run, execute(ctx, run))
            chunks = (await http.get(f"/documents/{did}/chunks", params={"pipeline_id": backend["pipeline"]})).json()
            assert chunks
            id = chunks[0]["id"]
            async with tools(backend) as client:
                assert not (await client.call_tool("fetch", {"id": id})).is_error
                assert (
                    await http.put(f"/documents/{did}/content", files={"file": ("new.md", b"replacement passage")})
                ).status_code == 200
                assert (await client.call_tool("fetch", {"id": id})).is_error
                pending = await client.call_tool("fetch", {"id": did})
                assert not pending.is_error and pending.structured_content["chunk_count"] == 0
                assert (await http.delete(f"/documents/{did}")).status_code == 202
                assert (await client.call_tool("fetch", {"id": id})).is_error
                assert (await client.call_tool("fetch", {"id": did})).is_error
    finally:
        ctx.start_workers(1)
