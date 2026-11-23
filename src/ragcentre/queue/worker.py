"""Worker threads and shut-down (section 12.3). Plain threads; everything synchronous."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from ragcentre.errors import ExecutionError
from ragcentre.queue.claims import (
    claim_run,
    claim_task,
    complete,
    complete_task,
    extend_lease,
    extend_task_lease,
    fail,
    fail_task,
    is_current,
    new_worker_id,
)
from ragcentre.queue.tasks import on_task_failed, run_task

if TYPE_CHECKING:
    from ragcentre.context import AppContext

log = logging.getLogger("ragcentre.worker")


class Worker:
    def __init__(self, ctx: AppContext, name: str, stop_event: threading.Event) -> None:
        self.ctx = ctx
        self.name = name
        self.stop_event = stop_event
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def _run(self) -> None:
        worker_id = new_worker_id()
        idle = 0.0
        while not self.stop_event.is_set():
            claimed_run = claim_run(self.ctx, worker_id)
            if claimed_run is not None:
                idle = 0.0
                self._execute_run(claimed_run, worker_id)
                continue
            task = claim_task(self.ctx, worker_id)
            if task is not None:
                idle = 0.0
                self._execute_task(task, worker_id)
                continue
            idle = min(idle + self.ctx.settings.idle_sleep_seconds, self.ctx.settings.max_idle_sleep_seconds)
            self.stop_event.wait(idle)

    def _execute_run(self, claim: Any, worker_id: str) -> None:
        stop_heartbeat = threading.Event()

        def heartbeat() -> None:
            while not stop_heartbeat.is_set():
                stop_heartbeat.wait(self.ctx.settings.heartbeat_seconds)
                if stop_heartbeat.is_set():
                    return
                if not extend_lease(self.ctx, claim):
                    return

        hb = threading.Thread(target=heartbeat, name=f"{self.name}-hb", daemon=True)
        hb.start()
        try:
            from ragcentre.pipeline.executors import execute

            stats = execute(self.ctx, claim)
            if is_current(self.ctx, claim):
                complete(self.ctx, claim, stats)
        except ExecutionError as exc:
            if is_current(self.ctx, claim):
                fail(self.ctx, claim, exc)
            else:
                log.info("stale result discarded", extra={"node_run_id": claim.id})
        except Exception:  # defensive: never lose a worker to a bug
            log.exception("executor crashed", extra={"node_run_id": claim.id})
            error = ExecutionError("internal_error", "Internal worker error. See server logs.", retryable=False)
            if is_current(self.ctx, claim):
                fail(self.ctx, claim, error)
        finally:
            stop_heartbeat.set()
            hb.join(timeout=1)

    def _heartbeat(self, extend: Any) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.ctx.settings.heartbeat_seconds):
                if not extend():
                    return

        thread = threading.Thread(target=beat, name=f"{self.name}-hb", daemon=True)
        thread.start()
        return stop, thread

    def _execute_task(self, task: Any, worker_id: str) -> None:
        task_id, attempt = int(task["id"]), int(task["attempt"])
        stop, beat = self._heartbeat(lambda: extend_task_lease(self.ctx, task_id, worker_id, attempt))
        try:
            run_task(self.ctx, task)
            complete_task(self.ctx, task_id, worker_id, attempt)
        except Exception as exc:
            if isinstance(exc, ExecutionError):
                error = exc
            else:
                log.exception("task crashed", extra={"task_id": task_id})
                error = ExecutionError("internal_error", "Internal worker error. See server logs.", retryable=False)
            fail_task(self.ctx, task_id, worker_id, attempt, error)
            if self.ctx.db.scalar("SELECT status FROM tasks WHERE id = ?", (task_id,)) == "failed":
                on_task_failed(self.ctx, task)
        finally:
            stop.set()
            beat.join(timeout=1)


class WorkerPool:
    def __init__(self, ctx: AppContext, size: int) -> None:
        self.ctx = ctx
        self.size = size
        self.stop_event = threading.Event()
        self.workers = [Worker(ctx, f"ragcentre-worker-{i}", self.stop_event) for i in range(size)]

    def start(self) -> None:
        for worker in self.workers:
            worker.start()

    def stop(self, timeout: float | None = None) -> None:
        self.stop_event.set()
        for worker in self.workers:
            worker.join(timeout)

    def __enter__(self) -> WorkerPool:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
