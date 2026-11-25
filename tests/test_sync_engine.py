from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

from ragcentre.connectors.base import FetchedFile, RemoteItem, ScanResult
from ragcentre.connectors.sync import run_sync_source
from ragcentre.context import AppContext
from ragcentre.errors import ExecutionError
from ragcentre.ingest import ingest_document

DEFAULT_TEAM = "00000000-0000-0000-0000-000000000001"


def _corpus(ctx: AppContext, name: str = "c") -> str:
    ctx.db.execute(
        "INSERT INTO corpora (id, name, metadata_schema, team_id, created_at) VALUES (?, ?, ?, ?, ?)",
        ("corpus-1", name, "{}", DEFAULT_TEAM, "2026-01-01T00:00:00.000000Z"),
    )
    ctx.db.execute(
        "INSERT INTO providers (id, name, kind, type, config, fingerprint, team_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("prov-1", "s3", "connector", "s3", "{}", "fp", DEFAULT_TEAM, "now", "now"),
    )
    return "corpus-1"


def _source(ctx: AppContext, corpus_id: str, scope: dict | None = None, schedule: int = 0) -> str:
    ctx.db.execute(
        "INSERT INTO sync_sources (id, corpus_id, provider_id, name, scope, schedule_seconds, status, "
        "metadata_template, attributes_template, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("source-1", corpus_id, "prov-1", "s", json.dumps(scope or {}), schedule, "syncing", "{}", "{}", "now", "now"),
    )
    return "source-1"


def _item(remote_id: str, version: str = "v1", *, deleted: bool = False) -> RemoteItem:
    return RemoteItem(
        remote_id=remote_id,
        name=remote_id,
        mime_type="text/plain",
        size=5,
        version=version,
        modified_at=None,
        deleted=deleted,
        path=remote_id,
    )


def _fetch(content: bytes, *, version: str = "v1") -> FetchedFile:
    return FetchedFile(
        content=content, filename="a.txt", mime_type="text/plain", version=version, metadata={}, attributes={}
    )


class FakeConnector:
    def __init__(self, pages: list[ScanResult], fetches: dict[str, FetchedFile] | None = None) -> None:
        self.pages = list(pages)
        self.fetches = fetches or {}
        self.scanned: list[str | None] = []
        self.fetched: list[str] = []

    def check(self) -> None:
        return None

    def scan(self, scope: dict, cursor: str | None) -> ScanResult:
        self.scanned.append(cursor)
        return self.pages.pop(0) if self.pages else ScanResult(items=iter(()), next_cursor=None, full=True)

    def fetch(self, scope: dict, item: RemoteItem) -> FetchedFile:
        self.fetched.append(item.remote_id)
        return self.fetches[item.remote_id]

    def close(self) -> None:
        return None


class ErrorConnector:
    def check(self) -> None:
        return None

    def scan(self, scope: dict, cursor: str | None) -> ScanResult:
        raise ExecutionError("provider_error", "boom", retryable=True)

    def fetch(self, scope: dict, item: RemoteItem) -> FetchedFile:
        raise AssertionError("not reached")

    def close(self) -> None:
        return None


class PagingErrorConnector:
    def __init__(self) -> None:
        self.calls = 0

    def scan(self, scope: dict, cursor: str | None) -> ScanResult:
        self.calls += 1
        if self.calls == 1:
            return ScanResult(items=iter(()), next_cursor="tok2", full=True)
        raise ExecutionError("provider_error", "boom", retryable=True)

    def fetch(self, scope: dict, item: RemoteItem) -> FetchedFile:
        raise AssertionError("not reached")

    def close(self) -> None:
        return None


class FeedConnector:
    """A change-feed connector: one page per sync whose resume token is persisted for next time."""

    def __init__(self, items: list[RemoteItem], fetches: dict[str, FetchedFile] | None = None) -> None:
        self.items = items
        self.fetches = fetches or {}
        self.scanned: list[str | None] = []

    def check(self) -> None:
        return None

    def scan(self, scope: dict, cursor: str | None) -> ScanResult:
        self.scanned.append(cursor)
        return ScanResult(items=iter(self.items), next_cursor=None, full=False, resume_cursor="delta-tok")

    def fetch(self, scope: dict, item: RemoteItem) -> FetchedFile:
        return self.fetches[item.remote_id]

    def close(self) -> None:
        return None


def _run(ctx: AppContext, connector: Any) -> None:
    @contextmanager
    def lease(provider_id: str):
        yield connector

    ctx.providers.lease = lease  # type: ignore[method-assign]
    run_sync_source(ctx, "source-1")


def test_new_item_ingests(ctx: AppContext) -> None:
    corpus_id = _corpus(ctx)
    _source(ctx, corpus_id)
    connector = FakeConnector(
        [ScanResult(items=iter([_item("a")]), next_cursor=None, full=True)],
        fetches={"a": _fetch(b"hello")},
    )
    _run(ctx, connector)

    assert connector.fetched == ["a"]
    doc_id = ctx.db.scalar("SELECT id FROM documents WHERE external_id = ?", ("source-1:a",))
    assert doc_id is not None
    row = ctx.db.one("SELECT * FROM source_items WHERE source_id = ? AND remote_id = ?", ("source-1", "a"))
    assert row["document_id"] == doc_id
    assert row["remote_version"] == "v1"
    assert ctx.db.scalar("SELECT status FROM sync_sources WHERE id = ?", ("source-1",)) == "idle"
    assert ctx.db.scalar("SELECT cursor FROM sync_sources WHERE id = ?", ("source-1",)) is None


def test_unchanged_item_is_skipped(ctx: AppContext) -> None:
    corpus_id = _corpus(ctx)
    _source(ctx, corpus_id)
    doc = ingest_document(ctx, corpus_id, b"old", filename="a.txt", mime_type="text/plain", external_id="source-1:a")
    ctx.db.execute(
        "INSERT INTO source_items (source_id, remote_id, remote_version, document_id, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("source-1", "a", "v1", doc.document_id, "now"),
    )
    connector = FakeConnector([ScanResult(items=iter([_item("a", "v1")]), next_cursor=None, full=True)])
    _run(ctx, connector)

    assert connector.fetched == []
    assert ctx.db.scalar("SELECT revision FROM documents WHERE id = ?", (doc.document_id,)) == 1


def test_changed_item_replaces_content(ctx: AppContext) -> None:
    corpus_id = _corpus(ctx)
    _source(ctx, corpus_id)
    doc = ingest_document(ctx, corpus_id, b"old", filename="a.txt", mime_type="text/plain", external_id="source-1:a")
    ctx.db.execute(
        "INSERT INTO source_items (source_id, remote_id, remote_version, document_id, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("source-1", "a", "v1", doc.document_id, "now"),
    )
    connector = FakeConnector(
        [ScanResult(items=iter([_item("a", "v2")]), next_cursor=None, full=True)],
        fetches={"a": _fetch(b"new", version="v2")},
    )
    _run(ctx, connector)

    assert connector.fetched == ["a"]
    assert ctx.db.scalar("SELECT revision FROM documents WHERE id = ?", (doc.document_id,)) == 2
    assert ctx.db.scalar("SELECT remote_version FROM source_items WHERE remote_id = ?", ("a",)) == "v2"


def test_full_list_deletes_vanished_item(ctx: AppContext) -> None:
    corpus_id = _corpus(ctx)
    _source(ctx, corpus_id)
    doc = ingest_document(ctx, corpus_id, b"x", filename="a.txt", mime_type="text/plain", external_id="source-1:gone")
    ctx.db.execute(
        "INSERT INTO source_items (source_id, remote_id, remote_version, document_id, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("source-1", "gone", "v1", doc.document_id, "now"),
    )
    connector = FakeConnector(
        [ScanResult(items=iter([_item("kept")]), next_cursor=None, full=True)],
        fetches={"kept": _fetch(b"kept")},
    )
    _run(ctx, connector)

    assert ctx.db.scalar("SELECT status FROM documents WHERE id = ?", (doc.document_id,)) == "deleting"
    assert ctx.db.scalar("SELECT COUNT(*) FROM source_items WHERE remote_id = ?", ("gone",)) == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'delete_document'") == 1


def test_deleted_feed_item_deletes_document(ctx: AppContext) -> None:
    corpus_id = _corpus(ctx)
    _source(ctx, corpus_id)
    doc = ingest_document(ctx, corpus_id, b"x", filename="a.txt", mime_type="text/plain", external_id="source-1:a")
    ctx.db.execute(
        "INSERT INTO source_items (source_id, remote_id, remote_version, document_id, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("source-1", "a", "v1", doc.document_id, "now"),
    )
    connector = FakeConnector([ScanResult(items=iter([_item("a", "v1", deleted=True)]), next_cursor=None, full=False)])
    _run(ctx, connector)

    assert ctx.db.scalar("SELECT status FROM documents WHERE id = ?", (doc.document_id,)) == "deleting"
    assert ctx.db.scalar("SELECT COUNT(*) FROM source_items WHERE remote_id = ?", ("a",)) == 0


def test_oversize_item_is_skipped_and_recorded(ctx: AppContext) -> None:
    corpus_id = _corpus(ctx)
    _source(ctx, corpus_id)
    ctx.settings = ctx.settings.with_(max_upload_mb=0)
    connector = FakeConnector(
        [ScanResult(items=iter([_item("a")]), next_cursor=None, full=True)],
        fetches={"a": _fetch(b"x")},
    )
    _run(ctx, connector)

    assert connector.fetched == ["a"]
    assert ctx.db.scalar("SELECT COUNT(*) FROM documents") == 0
    row = ctx.db.one("SELECT * FROM source_items WHERE source_id = ? AND remote_id = ?", ("source-1", "a"))
    assert row["skipped_reason"] == "too_large"
    assert row["document_id"] is None


def test_retryable_error_leaves_source_syncing(ctx: AppContext) -> None:
    _source(ctx, _corpus(ctx))
    with pytest.raises(ExecutionError):
        _run(ctx, ErrorConnector())
    assert ctx.db.scalar("SELECT status FROM sync_sources WHERE id = ?", ("source-1",)) == "syncing"


def test_interrupted_scan_persists_cursor(ctx: AppContext) -> None:
    _source(ctx, _corpus(ctx))
    with pytest.raises(ExecutionError):
        _run(ctx, PagingErrorConnector())
    assert ctx.db.scalar("SELECT cursor FROM sync_sources WHERE id = ?", ("source-1",)) == "tok2"
    assert ctx.db.scalar("SELECT status FROM sync_sources WHERE id = ?", ("source-1",)) == "syncing"


def test_feed_connector_persists_resume_cursor(ctx: AppContext) -> None:
    corpus_id = _corpus(ctx)
    _source(ctx, corpus_id)
    connector = FeedConnector([_item("a")], fetches={"a": _fetch(b"hello")})
    _run(ctx, connector)

    assert ctx.db.scalar("SELECT status FROM sync_sources WHERE id = ?", ("source-1",)) == "idle"
    assert ctx.db.scalar("SELECT cursor FROM sync_sources WHERE id = ?", ("source-1",)) == "delta-tok"
    assert ctx.db.scalar("SELECT COUNT(*) FROM documents WHERE external_id = ?", ("source-1:a",)) == 1


def test_cross_team_source_refused_before_lease(ctx: AppContext) -> None:
    """A previously cross-wired source must fail closed before any connector lease/network call."""
    ctx.db.execute(
        "INSERT INTO teams (id, name, slug, created_at) VALUES (?, ?, ?, ?)", ("team-b", "Beta", "beta", "now")
    )
    ctx.db.execute(
        "INSERT INTO corpora (id, name, metadata_schema, team_id, created_at) VALUES (?, ?, ?, ?, ?)",
        ("corpus-a", "A", "{}", DEFAULT_TEAM, "now"),
    )
    ctx.db.execute(
        "INSERT INTO providers (id, name, kind, type, config, fingerprint, team_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("prov-b", "b-s3", "connector", "s3", "{}", "fp-b", "team-b", "now", "now"),
    )
    ctx.db.execute(
        "INSERT INTO sync_sources (id, corpus_id, provider_id, name, scope, schedule_seconds, status, "
        "metadata_template, attributes_template, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("source-x", "corpus-a", "prov-b", "x", "{}", 0, "syncing", "{}", "{}", "now", "now"),
    )
    with pytest.raises(ExecutionError) as exc:
        run_sync_source(ctx, "source-x")
    assert exc.value.code == "invalid_source"
    assert exc.value.retryable is False
