"""Support for the OCR host contract tests (``test_ocr_options_contract``, ``test_ocr_routing_contract``).

``ShimFarm`` fakes OCR shims on ``httpx.MockTransport``. Each engine is a port on ``parser.test``, so
two providers can be told apart by the text they return. A farm behaves like an
``examples/shimkit`` service that declares its options: it publishes ``options_schema`` on
``/info``, and ``POST /parse`` answers ``400 invalid_options`` for an unknown, mistyped or out of
range option before it does any work. That check is written here on its own, so it does not borrow
the host's validator and the 400 path the tests reach is real.

``Host`` drives RAGCentre through its HTTP API only.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ragcentre.api.app import create_app
from ragcentre.context import AppContext, open_context
from ragcentre.providers.parsers import http as parser_http
from ragcentre.settings import Settings
from ragcentre.testing.fakes import FakeServices, _multipart

ENGINE_A = "http://parser.test:8701"
ENGINE_B = "http://parser.test:8702"
LABEL_A = "engine-8701"
LABEL_B = "engine-8702"
# A page the OCR check accepts as read (it wants the code and three of its words).
PROBE_TEXT = "RAGCentre OCR check. This page is a scanned image. PUMP 4827. four bolts, inlet valve, pressure."

OPTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ocr": {"type": "boolean", "default": True},
        "tables": {"type": "string", "enum": ["fast", "accurate"], "default": "fast"},
        "dpi": {"type": "integer", "minimum": 72, "maximum": 400, "default": 192},
        "page_range": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2, "maxItems": 2},
    },
}

# What the fake engine returns, so the tests can compare the parsed output with something they know.
HEADING_BBOX = [72.0, 80.0, 540.0, 104.0]
TABLE_CODE = "4827"  # only in the output of ``tables: accurate``


def engine_ir(label: str, options: dict[str, Any]) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "heading",
            "level": 1,
            "text": "Pump manual",
            "page": 1,
            "bbox": HEADING_BBOX,
            "metadata": {"confidence": 0.98},
        },
        {
            "type": "paragraph",
            "text": f"Read by {label}.",
            "page": 1,
            "bbox": [72.0, 120.0, 540.0, 160.0],
            "metadata": {"confidence": 0.5},
        },
        {
            "type": "paragraph",
            "text": "Closing remarks.",
            "page": 2,
            "bbox": [72.0, 90.0, 540.0, 130.0],
            "metadata": {"confidence": 0.75},
        },
    ]
    if options.get("tables") == "accurate":
        blocks.append(
            {
                "type": "table",
                "text": f"| part | code |\n| --- | --- |\n| pump | {TABLE_CODE} |",
                "html": f"<table><tr><td>pump</td><td>{TABLE_CODE}</td></tr></table>",
                "page": 2,
                "bbox": [72.0, 140.0, 540.0, 220.0],
                "metadata": {"confidence": 0.91},
            }
        )
    return {
        "schema_version": "1.0",
        "metadata": {"page_count": 2},
        "pages": [{"page": n, "width": 612.0, "height": 792.0} for n in (1, 2)],
        "blocks": blocks,
    }


def option_problem(options: dict[str, Any]) -> str | None:
    """Why a shim that declares ``OPTIONS_SCHEMA`` refuses ``options``, or ``None``. Same rules and
    wording as ``shimkit.parse_options`` (strict: an undeclared option is an error)."""
    declared = OPTIONS_SCHEMA["properties"]
    unknown = sorted(set(options) - set(declared))
    if unknown:
        return f"unknown option {', '.join(map(repr, unknown))}; the options are: {', '.join(declared)}"
    for key, value in options.items():
        spec = declared[key]
        kind = spec["type"]
        if kind == "boolean" and not isinstance(value, bool):
            return f"option {key!r} must be true or false"
        if kind == "string" and value not in spec["enum"]:
            return f"option {key!r} must be one of: {', '.join(spec['enum'])}"
        if kind == "integer" and (
            isinstance(value, bool) or not isinstance(value, int) or not spec["minimum"] <= value <= spec["maximum"]
        ):
            return f"option {key!r} must be an integer from {spec['minimum']} to {spec['maximum']}"
        if kind == "array":
            ok = (
                isinstance(value, list)
                and len(value) == 2
                and all(isinstance(i, int) and not isinstance(i, bool) and i >= 1 for i in value)
            )
            if not ok:
                return f"option {key!r} must be [first, last] with 1-based page numbers"
    return None


@dataclass(frozen=True)
class ParseCall:
    """One ``POST /parse`` as the shim received it."""

    engine: str
    filename: str | None
    mime: str
    options_field: bytes  # the multipart ``options`` field, as it arrived

    @property
    def options(self) -> Any:
        return json.loads(self.options_field)


class ShimFarm(FakeServices):
    """``FakeServices`` whose parser is one or more OCR shims that enforce their ``options_schema``."""

    def __init__(self) -> None:
        super().__init__()
        # What /info publishes. None is a shim that publishes no schema; it still enforces OPTIONS_SCHEMA.
        self.parser_options_schema = OPTIONS_SCHEMA
        self.parse_calls: list[ParseCall] = []
        self.answer: dict[str, Any] | None = None  # when set, every parse returns this IR
        self.refuse: set[str] = set()  # MIME types the shim answers 415 for

    def parses(self, engine: str | None = None) -> list[ParseCall]:
        with self.lock:
            return [c for c in self.parse_calls if engine is None or c.engine == engine]

    @staticmethod
    def label(request: httpx.Request) -> str:
        return f"engine-{request.url.port}" if request.url.port else "engine"

    def _parser_info(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(super()._parser_info(request).content)
        body["name"] = self.label(request)
        return httpx.Response(200, json=body)

    def _parser_parse(self, request: httpx.Request) -> httpx.Response:
        form = _multipart(request)
        filename, mime, _content = form.get("file", (None, "application/octet-stream", b""))
        field = form.get("options", (None, "", b"{}"))[2]
        label = self.label(request)
        with self.lock:
            self.parse_calls.append(ParseCall(label, filename, mime, field))
        options = json.loads(field or b"{}")
        problem = option_problem(options)
        if problem is not None:  # the shim checks before any work, and before it takes a job
            return httpx.Response(400, json={"error": {"code": "invalid_options", "message": problem}})
        if mime in self.refuse:
            return httpx.Response(415, json={"error": {"code": "unsupported_media_type", "message": mime}})
        ir = self.answer if self.answer is not None else engine_ir(label, options)
        if self.parser_async and request.url.params.get("mode") == "async":
            with self.lock:
                job_id = f"job-{len(self.jobs) + 1}"
                self.jobs[job_id] = {"polls": 0, "ir": ir}
            return httpx.Response(202, json={"job_id": job_id, "poll_after_seconds": 0.5})
        return httpx.Response(200, json=ir)


def route(mime: str, provider: str, **options: Any) -> dict[str, Any]:
    return {"mime": mime, "provider": provider, "options": options}


def pipeline_body(name: str, routes: list[dict[str, Any]], chunk_chars: int = 800) -> dict[str, Any]:
    return {
        "name": name,
        "nodes": [
            {"id": "parse", "type": "parse", "routes": routes},
            {
                "id": "chunk",
                "type": "chunk",
                "inputs": ["parse"],
                "strategy": "structure",
                "options": {"max_chars": chunk_chars, "overlap_chars": 0, "include_heading_path": True},
            },
            {"id": "embed", "type": "embed", "inputs": ["chunk"], "provider": "hash"},
            {
                "id": "vec",
                "type": "index_vector",
                "inputs": ["embed"],
                "provider": "store",
                "options": {"metric": "cosine"},
            },
            {"id": "fts", "type": "index_fts", "inputs": ["chunk"], "options": {"tokenizer": "unicode61"}},
        ],
        "retrieval": {
            "sources": [{"id": "dense", "index": "vec", "top_k": 30}, {"id": "sparse", "index": "fts", "top_k": 30}],
            "fusion": {"method": "rrf", "k": 60},
            "top_k": 8,
        },
    }


class Host:
    """A running RAGCentre (API plus background workers) and the shim farm it talks to."""

    def __init__(self, client: TestClient, ctx: AppContext, farm: ShimFarm) -> None:
        self.client, self.ctx, self.farm = client, ctx, farm
        ctx.start_workers(2)
        self.post("/v1/providers", name="hash", kind="embedder", type="builtin_hash", config={})
        self.post("/v1/providers", name="store", kind="vector_store", type="sqlite_vec", config={})
        self.corpus_id = self.post("/v1/corpora", name="ocr")["id"]

    def post(self, path: str, status: int = 201, **body: Any) -> Any:
        r = self.client.post(path, json=body)
        assert r.status_code == status, f"POST {path} -> {r.status_code}: {r.text}"
        return r.json()

    def get(self, path: str, **params: Any) -> Any:
        r = self.client.get(path, params=params)
        assert r.status_code == 200, f"GET {path} -> {r.status_code}: {r.text}"
        return r.json()

    def add_parser(self, name: str, base_url: str = ENGINE_A, **config: Any) -> dict[str, Any]:
        created: dict[str, Any] = self.post(
            "/v1/providers", name=name, kind="parser", type="http", config={"base_url": base_url, **config}
        )
        return created

    def upload(self, filename: str, content: bytes = b"page", mime: str = "application/pdf") -> dict[str, Any]:
        r = self.client.post(
            f"/v1/corpora/{self.corpus_id}/documents",
            files={"file": (filename, content, mime)},
            data={"metadata": "{}", "attributes": "{}"},
        )
        assert r.status_code == 201, r.text
        document: dict[str, Any] = r.json()
        return document

    def try_create(self, body: dict[str, Any], *, dry_run: bool = False) -> httpx.Response:
        response: httpx.Response = self.client.post(
            f"/v1/corpora/{self.corpus_id}/pipelines", json=body, params={"dry_run": "true"} if dry_run else {}
        )
        return response

    def pipeline(self, body: dict[str, Any]) -> str:
        """Create a pipeline and wait until its work is done (built, or failed for good)."""
        r = self.try_create(body)
        assert r.status_code == 201, r.text
        pipeline_id = str(r.json()["id"])
        self.settle(pipeline_id)
        return pipeline_id

    def settle(self, pipeline_id: str, timeout: float = 15.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            state = self.get(f"/v1/pipelines/{pipeline_id}/status")
            if state["status"] != "building":
                return state  # type: ignore[no-any-return]
            if time.monotonic() > deadline:
                raise AssertionError(f"pipeline still building after {timeout}s: {state}")
            time.sleep(0.02)

    def parse_node_fp(self, pipeline_id: str) -> str:
        nodes = self.get(f"/v1/pipelines/{pipeline_id}")["nodes"]
        return str(next(n["node_fp"] for n in nodes if n["type"] == "parse"))

    def parse_run(self, document_id: str, pipeline_id: str) -> dict[str, Any]:
        runs = self.get("/v1/node-runs", document_id=document_id, pipeline_id=pipeline_id)["items"]
        (run,) = [r for r in runs if r["node_type"] == "parse"]
        return run  # type: ignore[no-any-return]

    def parsed(self, document_id: str, pipeline_id: str) -> dict[str, Any]:
        return self.get(f"/v1/documents/{document_id}/parsed", pipeline_id=pipeline_id)  # type: ignore[no-any-return]

    def texts(self, document_id: str, pipeline_id: str) -> list[str]:
        return [b["text"] for b in self.parsed(document_id, pipeline_id)["blocks"]]

    def retry_parse(self, document_id: str, pipeline_id: str) -> dict[str, Any]:
        """Retry the document's parse run, as a user does from the activity view, and wait for it."""
        run = self.parse_run(document_id, pipeline_id)
        r = self.client.post(f"/v1/node-runs/{run['id']}/retry")
        assert r.status_code == 200, r.text
        deadline = time.monotonic() + 15
        while True:
            now = self.parse_run(document_id, pipeline_id)
            if now["status"] in ("succeeded", "failed"):
                return now
            if time.monotonic() > deadline:
                raise AssertionError(f"parse run still {now['status']} after retry: {now}")
            time.sleep(0.02)

    def query(self, pipeline_id: str, text: str) -> list[str]:
        """The passages a query returns from one pipeline."""
        body = self.post(f"/v1/corpora/{self.corpus_id}/query", status=200, query=text, pipeline_id=pipeline_id)
        return [str(hit["text"]) for hit in body["results"]]


def canonical(value: Any) -> str:
    """JSON with sorted keys, so ``false`` and ``0`` stay different and key order does not matter."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@contextmanager
def open_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    """RAGCentre on a fresh data directory, with workers, talking to a new ``ShimFarm`` (``host.farm``).

    Workers poll quickly, nothing goes looking for shims on the network, and an async job is polled
    without waiting out its poll interval."""
    monkeypatch.setattr(parser_http, "_sleep", lambda seconds: None)
    farm = ShimFarm()
    settings = Settings(
        data_dir=tmp_path / "data",
        workers=0,
        discovery_urls="off",
        idle_sleep_seconds=0.01,
        max_idle_sleep_seconds=0.02,
    )
    ctx = open_context(settings, http_client_factory=farm.client_factory)
    try:
        with TestClient(create_app(ctx)) as client:
            yield Host(client, ctx, farm)
    finally:
        ctx.close()
