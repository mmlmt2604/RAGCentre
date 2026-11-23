"""An in-memory stand-in for RAGCentre's read API, shaped like its real responses."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

MANUALS = {
    "id": "c-manuals",
    "name": "manuals",
    "metadata_schema": {"fields": {"audience": {"type": "str", "default": "customer"}}},
    "active_pipeline_id": "p-1",
    "document_count": 1,
    "pipelines": [],
    "created_at": "2026-11-01T00:00:00Z",
}
HR = {
    "id": "c-hr",
    "name": "hr",
    "metadata_schema": {"fields": {}},
    "active_pipeline_id": "p-2",
    "document_count": 1,
    "pipelines": [],
    "created_at": "2026-11-01T00:00:00Z",
}
EMPTY = {
    "id": "c-empty",
    "name": "empty",
    "metadata_schema": {"fields": {}},
    "active_pipeline_id": None,
    "document_count": 0,
    "pipelines": [],
    "created_at": "2026-11-01T00:00:00Z",
}


def chunk(ordinal: int, doc: str = "d-valve", text: str | None = None) -> dict[str, Any]:
    return {
        "id": f"k-{doc}-{ordinal}",
        "document_id": doc,
        "ordinal": ordinal,
        "text": text if text is not None else f"Passage {ordinal} of {doc}.",
        "context": None,
        "page_start": ordinal + 1,
        "page_end": ordinal + 1,
        "heading_path": ["Maintenance", "Torque"],
        "block_ids": [],
        "char_count": 20,
    }


DOCS = {
    "d-valve": {
        "id": "d-valve",
        "corpus_id": "c-manuals",
        "filename": "valve.pdf",
        "external_id": None,
        "metadata": {"audience": "staff"},
    },
    "d-leave": {"id": "d-leave", "corpus_id": "c-hr", "filename": "leave.md", "external_id": "HR-7", "metadata": {}},
}
CHUNKS = {d: [chunk(i, d) for i in range(6)] for d in DOCS}


def err(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message, "details": {}}})


@dataclass
class FakeApi:
    version: str = "0.0.3"
    scope: str = "read"
    corpora: list[dict[str, Any]] = field(default_factory=lambda: [MANUALS, HR, EMPTY])
    whoami_status: int = 200
    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    query_truncated: bool = False
    chunk_text: dict[str, str] = field(default_factory=dict)
    corpora_status: int = 200
    whoami_down: bool = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        parts = path.strip("/").split("/")
        if request.method == "GET" and path == "/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "version": self.version,
                    "migration": 9,
                    "workers": 1,
                    "auth_required": True,
                    "secret_storage_enabled": False,
                },
            )
        if request.method == "GET" and path == "/auth/whoami":
            if self.whoami_down:
                raise httpx.ConnectError("refused")
            if self.whoami_status != 200:
                return err(self.whoami_status, "unauthorized", "Bad token.")
            return httpx.Response(200, json={"scope": self.scope})
        if request.method == "GET" and path == "/corpora":
            if self.corpora_status != 200:
                return err(self.corpora_status, "internal_error", "Database is locked.")
            return httpx.Response(200, json={"items": self.corpora, "next_cursor": None})
        if request.method == "POST" and len(parts) == 3 and parts[0] == "corpora" and parts[2] == "query":
            return self.query(parts[1], body)
        if request.method == "GET" and len(parts) == 2 and parts[0] == "chunks":
            return self.chunk(
                parts[1], int(request.url.params.get("before", 0)), int(request.url.params.get("after", 0))
            )
        if request.method == "GET" and len(parts) == 3 and parts[0] == "documents" and parts[2] == "outline":
            return self.outline(parts[1])
        return err(404, "not_found", "No such route.")

    def query(self, corpus_id: str, body: dict[str, Any]) -> httpx.Response:
        corpus = next((c for c in self.corpora if c["id"] == corpus_id), None)
        if corpus is None:
            return err(404, "not_found", "Corpus not found.")
        if corpus["active_pipeline_id"] is None:
            return err(404, "not_found", "This corpus has no active pipeline; pass pipeline_id explicitly.")
        flt = body.get("filter")
        if flt and any(k not in corpus["metadata_schema"]["fields"] and not k.startswith("$") for k in flt):
            return err(422, "invalid_filter", f"Unknown field '{next(iter(flt))}'.")
        doc_id = "d-valve" if corpus_id == "c-manuals" else "d-leave"
        doc = DOCS[doc_id]
        results = []
        for rank, c in enumerate(CHUNKS[doc_id][: body["top_k"]], start=1):
            results.append(
                {
                    "chunk_id": c["id"],
                    "document_id": doc_id,
                    "score": 1 / rank,
                    "rank": rank,
                    "ordinal": c["ordinal"],
                    "text": self.chunk_text.get(c["id"], c["text"]),
                    "context": None,
                    "page_start": c["page_start"],
                    "page_end": c["page_end"],
                    "heading_path": c["heading_path"],
                    "document": {
                        "filename": doc["filename"],
                        "external_id": doc["external_id"],
                        "metadata": doc["metadata"],
                        "attributes": {},
                    },
                    "context_before": [],
                    "context_after": [],
                    "sources": {},
                }
            )
        return httpx.Response(
            200,
            json={
                "pipeline_id": corpus["active_pipeline_id"],
                "pipeline_fingerprint": "fp",
                "partial": False,
                "results": results,
                "timings_ms": {},
                "truncated": self.query_truncated,
            },
        )

    def chunk(self, chunk_id: str, before: int, after: int) -> httpx.Response:
        for doc_id, chunks in CHUNKS.items():
            for c in chunks:
                if c["id"] == chunk_id:
                    o = c["ordinal"]
                    with_text = [{**x, "text": self.chunk_text.get(x["id"], x["text"])} for x in chunks]
                    return httpx.Response(
                        200,
                        json={
                            "chunk": with_text[o],
                            "before": with_text[max(0, o - before) : o],
                            "after": with_text[o + 1 : o + 1 + after],
                            "document": DOCS[doc_id],
                        },
                    )
        return err(404, "not_found", "Chunk not found.")

    def outline(self, document_id: str) -> httpx.Response:
        if document_id not in DOCS:
            return err(404, "not_found", "Document not found.")
        return httpx.Response(
            200,
            json={
                "document": {**DOCS[document_id], "mime_type": "application/pdf"},
                "chunk_count": 6,
                "page_count": 6,
                "sections": [
                    {
                        "heading_path": ["Overview"],
                        "first_chunk_id": f"k-{document_id}-0",
                        "page_start": 1,
                        "page_end": 2,
                        "chunk_count": 2,
                    },
                    {
                        "heading_path": ["Maintenance", "Torque"],
                        "first_chunk_id": f"k-{document_id}-2",
                        "page_start": 3,
                        "page_end": 6,
                        "chunk_count": 4,
                    },
                ],
                "truncated": False,
            },
        )
