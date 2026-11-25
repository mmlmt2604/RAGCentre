"""A poll scheduler for sync sources, started beside the ingestion workers.

Only the ``serve`` command starts this (see the plan's D7): the worker command has no scheduler.
``enqueue_sync`` is the compare-and-set, so even if two schedulers were somehow started, a source
would still be enqueued once.
"""

from __future__ import annotations

import logging
import threading

from ragcentre.connectors.sync import enqueue_sync
from ragcentre.context import AppContext
from ragcentre.timeutil import now_iso

log = logging.getLogger("ragcentre.connectors.scheduler")


class Scheduler:
    def __init__(self, ctx: AppContext, stop_event: threading.Event | None = None) -> None:
        self.ctx = ctx
        self.stop_event = stop_event or threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name="ragcentre-scheduler", daemon=True)
        self.thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("scheduler tick failed")
            self.stop_event.wait(self.ctx.settings.scheduler_tick_seconds)

    def _tick(self) -> None:
        now = now_iso()
        rows = self.ctx.db.all(
            "SELECT id FROM sync_sources WHERE status != 'syncing' AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? ORDER BY next_run_at",
            (now,),
        )
        for row in rows:
            if self.stop_event.is_set():
                return
            enqueue_sync(self.ctx, row["id"])
