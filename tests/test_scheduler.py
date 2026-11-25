from __future__ import annotations

from ragcentre.connectors.scheduler import Scheduler
from ragcentre.connectors.sync import enqueue_sync
from ragcentre.context import AppContext
from ragcentre.timeutil import after, now_iso

DEFAULT_TEAM = "00000000-0000-0000-0000-000000000001"


def _setup(ctx: AppContext) -> None:
    ctx.db.execute(
        "INSERT INTO corpora (id, name, metadata_schema, team_id, created_at) VALUES (?, ?, ?, ?, ?)",
        ("corpus-1", "c", "{}", DEFAULT_TEAM, now_iso()),
    )
    ctx.db.execute(
        "INSERT INTO providers (id, name, kind, type, config, fingerprint, team_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("prov-1", "s3", "connector", "s3", "{}", "fp", DEFAULT_TEAM, now_iso(), now_iso()),
    )


def _source(ctx: AppContext, source_id: str, *, status: str = "idle", next_run_at: str | None = None) -> None:
    ctx.db.execute(
        "INSERT INTO sync_sources (id, corpus_id, provider_id, name, scope, schedule_seconds, status, "
        "next_run_at, metadata_template, attributes_template, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source_id, "corpus-1", "prov-1", source_id, "{}", 3600, status, next_run_at, "{}", "{}", now_iso(), now_iso()),
    )


def test_scheduler_enqueues_only_due_sources(ctx: AppContext) -> None:
    _setup(ctx)
    _source(ctx, "due", next_run_at=after(-1))
    _source(ctx, "future", next_run_at=after(3600))
    _source(ctx, "manual", next_run_at=None)

    Scheduler(ctx)._tick()

    assert ctx.db.scalar("SELECT status FROM sync_sources WHERE id = 'due'") == "syncing"
    assert ctx.db.scalar("SELECT status FROM sync_sources WHERE id = 'future'") == "idle"
    assert ctx.db.scalar("SELECT status FROM sync_sources WHERE id = 'manual'") == "idle"
    assert ctx.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'sync_source'") == 1


def test_enqueue_sync_is_a_compare_and_set(ctx: AppContext) -> None:
    _setup(ctx)
    _source(ctx, "s")

    assert enqueue_sync(ctx, "s") is True
    assert enqueue_sync(ctx, "s") is False
    assert ctx.db.scalar("SELECT status FROM sync_sources WHERE id = 's'") == "syncing"
    assert ctx.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'sync_source'") == 1
