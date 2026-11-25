"""The single document write path, shared by the upload route and data connectors.

The upload route (``api/routers/documents.py``) and the connector sync engine must both insert or
update a document in exactly the same way, otherwise the two drift. This module holds that logic;
the route keeps only the multipart plumbing.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from ragcentre.context import AppContext
from ragcentre.errors import Conflict, ErrorCode, NotFound, RagError
from ragcentre.hashing import new_id
from ragcentre.locks import document_lock
from ragcentre.models.metadata import validate_metadata
from ragcentre.pipeline.planner import on_document_created, on_document_replaced, reset_vector_runs
from ragcentre.pipeline.purge import release_document
from ragcentre.timeutil import now_iso


@dataclass(frozen=True)
class IngestResult:
    document_id: str
    created: bool  # True = inserted; False = updated/replaced an existing row


def ingest_document(
    ctx: AppContext,
    corpus_id: str,
    content: bytes,
    *,
    filename: str,
    mime_type: str,
    external_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
) -> IngestResult:
    """Insert or upsert a document by ``external_id`` and schedule its pipeline runs.

    ``metadata`` and ``attributes`` of ``None`` mean "leave untouched" on an update (an insert uses
    ``{}``); a non-``None`` value is schema-validated (metadata) and stored.
    """
    if len(content) > ctx.settings.max_upload_bytes:
        raise RagError("File is larger than the upload limit.", code=ErrorCode.PAYLOAD_TOO_LARGE, status=413)
    corpus = ctx.db.one("SELECT * FROM corpora WHERE id = ?", (corpus_id,))
    if corpus is None:
        raise NotFound(f"Corpus {corpus_id} was not found.")
    schema = json.loads(corpus["metadata_schema"])
    meta = metadata if metadata is not None else {}
    attrs = attributes if attributes is not None else {}
    if metadata is not None:
        validate_metadata(meta, schema)
    doc_id = new_id()
    now = now_iso()
    # Serialize external-id upserts before acquiring the existing document's lock.
    external_lock = (
        document_lock(ctx.settings.data_dir, f"external:{corpus_id}:{external_id}")
        if external_id is not None
        else nullcontext()
    )
    created = False
    with external_lock:
        existing_id = ctx.db.scalar(
            "SELECT id FROM documents WHERE corpus_id = ? AND external_id = ?", (corpus_id, external_id)
        )
        with document_lock(ctx.settings.data_dir, existing_id or doc_id), ctx.db.write() as conn:
            existing = conn.execute("SELECT * FROM documents WHERE id = ?", (existing_id,)).fetchone()
            if existing is not None and existing["status"] != "active":
                raise Conflict("Document is being deleted.")
            sha = ctx.blobs.put(content)
            if existing is not None:
                doc_id = existing["id"]
                changed_content = existing["content_sha256"] != sha
                if changed_content:
                    release_document(conn, existing)
                if changed_content or metadata is not None or attributes is not None:
                    conn.execute(
                        "UPDATE documents SET content_sha256 = ?, filename = ?, mime_type = ?, size_bytes = ?, "
                        "metadata = ?, attributes = ?, updated_at = ? WHERE id = ?",
                        (
                            sha,
                            filename if changed_content else existing["filename"],
                            mime_type if changed_content else existing["mime_type"],
                            len(content),
                            json.dumps(meta) if metadata is not None else existing["metadata"],
                            json.dumps(attrs) if attributes is not None else existing["attributes"],
                            now,
                            doc_id,
                        ),
                    )
                    if changed_content:
                        on_document_replaced(conn, corpus_id, doc_id)
                    elif metadata is not None:
                        reset_vector_runs(conn, corpus_id, doc_id)
            else:
                validate_metadata(meta, schema)
                conn.execute(
                    "INSERT INTO documents (id, corpus_id, external_id, filename, mime_type, size_bytes, "
                    "content_sha256, metadata, attributes, revision, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)",
                    (
                        doc_id,
                        corpus_id,
                        external_id,
                        filename,
                        mime_type,
                        len(content),
                        sha,
                        json.dumps(meta),
                        json.dumps(attrs),
                        now,
                        now,
                    ),
                )
                on_document_created(conn, corpus_id, doc_id)
                created = True
    return IngestResult(document_id=doc_id, created=created)
