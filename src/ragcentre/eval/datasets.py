"""Evaluation dataset import (section 14.2): JSON Lines, one item per line."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from ragcentre.errors import Conflict, InvalidFilter, NotFound, ValidationFailed
from ragcentre.hashing import new_id
from ragcentre.models.filters import parse_filter, schema_types
from ragcentre.timeutil import now_iso

if TYPE_CHECKING:
    from ragcentre.context import AppContext

MAX_ERRORS = 100


def _document_index(ctx: AppContext, corpus_id: str) -> dict[str, tuple[str, str]]:
    """Map external_id and id to (document id, label)."""
    out: dict[str, tuple[str, str]] = {}
    rows = ctx.db.all(
        "SELECT id, external_id, filename FROM documents WHERE corpus_id = ? AND status = 'active'", (corpus_id,)
    )
    for r in rows:
        label = r["external_id"] or r["filename"]
        out[r["id"]] = (r["id"], label)
    for r in rows:  # external ids win over ids on the (unlikely) collision
        if r["external_id"]:
            out[r["external_id"]] = (r["id"], r["external_id"])
    return out


def _parse_item(raw: Any, docs: dict[str, tuple[str, str]], schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("each line must be a JSON object")
    unknown = set(raw) - {"question", "targets", "filter", "metadata"}
    if unknown:
        raise ValueError(f"unknown keys: {', '.join(sorted(unknown))}")
    question = raw.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("'question' must be a non-empty string")
    targets = raw.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("'targets' must be a non-empty list")
    resolved: list[dict[str, Any]] = []
    for i, t in enumerate(targets):
        if not isinstance(t, dict) or not isinstance(t.get("document"), str):
            raise ValueError(f"targets[{i}] needs a 'document' (external_id or id)")
        extra = set(t) - {"document", "pages", "text_contains"}
        if extra:
            raise ValueError(f"targets[{i}] has unknown keys: {', '.join(sorted(extra))}")
        doc = docs.get(t["document"])
        if doc is None:
            raise ValueError(f"targets[{i}]: unknown document {t['document']!r}")
        pages = t.get("pages")
        if pages is not None and (
            not isinstance(pages, list)
            or not pages
            or not all(isinstance(p, int) and not isinstance(p, bool) and p >= 1 for p in pages)
        ):
            raise ValueError(f"targets[{i}].pages must be a non-empty list of page numbers (1-based)")
        snippet = t.get("text_contains")
        if snippet is not None and (not isinstance(snippet, str) or not snippet.strip()):
            raise ValueError(f"targets[{i}].text_contains must be a non-empty string")
        resolved.append({"document_id": doc[0], "document_label": doc[1], "pages": pages, "text_contains": snippet})
    filt = raw.get("filter")
    if filt is not None:
        try:
            parse_filter(filt, schema_types(schema))
        except InvalidFilter as exc:
            raise ValueError(f"invalid filter: {exc.message}") from None
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("'metadata' must be an object")
    return {"question": question, "targets": resolved, "filter": filt or None, "metadata": metadata}


def import_dataset(ctx: AppContext, corpus_id: str, name: str, content: str) -> str:
    corpus = ctx.db.one("SELECT metadata_schema FROM corpora WHERE id = ?", (corpus_id,))
    if corpus is None:
        raise NotFound(f"Corpus {corpus_id} was not found.")
    if not name.strip():
        raise ValidationFailed("Give the dataset a name.")
    schema = json.loads(corpus["metadata_schema"])
    docs = _document_index(ctx, corpus_id)
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            items.append(_parse_item(json.loads(line), docs, schema))
        except json.JSONDecodeError as exc:
            errors.append({"line": line_no, "message": f"invalid JSON: {exc.msg}"})
        except ValueError as exc:
            errors.append({"line": line_no, "message": str(exc)})
        if len(errors) >= MAX_ERRORS:
            break
    if not items and not errors:
        errors.append({"line": 0, "message": "the file contains no items"})
    if errors:
        first = errors[0]
        raise ValidationFailed(
            f"The dataset has {len(errors)} problem(s); line {first['line']}: {first['message']}",
            details={"errors": errors},
        )
    dataset_id = new_id()
    try:
        with ctx.db.write() as conn:
            conn.execute(
                "INSERT INTO eval_datasets (id, corpus_id, name, created_at) VALUES (?, ?, ?, ?)",
                (dataset_id, corpus_id, name, now_iso()),
            )
            conn.executemany(
                "INSERT INTO eval_items (id, dataset_id, question, targets, filter, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        new_id(),
                        dataset_id,
                        it["question"],
                        json.dumps(it["targets"]),
                        json.dumps(it["filter"]) if it["filter"] is not None else None,
                        json.dumps(it["metadata"]),
                    )
                    for it in items
                ],
            )
    except sqlite3.IntegrityError:
        raise Conflict(f"A dataset named {name!r} already exists in this corpus.") from None
    return dataset_id


def items(ctx: AppContext, dataset_id: str) -> list[dict[str, Any]]:
    """Items in import order (rowid order)."""
    rows = ctx.db.all("SELECT * FROM eval_items WHERE dataset_id = ? ORDER BY rowid", (dataset_id,))
    return [
        {
            "id": r["id"],
            "question": r["question"],
            "targets": json.loads(r["targets"]),
            "filter": json.loads(r["filter"]) if r["filter"] else None,
            "metadata": json.loads(r["metadata"]),
        }
        for r in rows
    ]
