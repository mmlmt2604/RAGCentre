"""The application context: settings, databases and long-lived services.

Created once per process by ``open_context`` (the CLI and tests), handed to ``create_app``,
and closed after the server stops. Keeping start-up and shut-down outside the ASGI lifespan
keeps the whole codebase synchronous.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from ragcentre import __version__
from ragcentre.db.connection import Database
from ragcentre.db.migrate import migrate, migrate_cache, verify
from ragcentre.settings import Settings

if TYPE_CHECKING:
    from ragcentre.blobs import BlobStore
    from ragcentre.embedding_service import EmbeddingService
    from ragcentre.providers.manager import ProviderManager
    from ragcentre.secrets import SecretBox

log = logging.getLogger("ragcentre")

HttpClientFactory = Callable[..., httpx.Client]


def default_http_client_factory(**kwargs: Any) -> httpx.Client:
    return httpx.Client(**kwargs)


class AppContext:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client_factory: HttpClientFactory | None = None,
    ) -> None:
        self.settings = settings
        self.version = __version__
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(settings.db_path)
        self.cache = Database(settings.cache_path)
        self.http_client_factory: HttpClientFactory = http_client_factory or default_http_client_factory
        self.migration = 0
        self.workers: Any = None
        self._services: dict[str, Any] = {}
        self._services_lock = threading.RLock()

    # ---- start-up ---------------------------------------------------------------------------

    def initialise(self) -> None:
        applied = migrate(self.db)
        if applied:
            log.info("applied migrations", extra={"versions": applied})
        self.migration = verify(self.db)
        migrate_cache(self.cache)
        self._bootstrap()

    def _bootstrap(self) -> None:
        """Create built-in rows (the builtin-text provider)."""
        from ragcentre.providers.manager import ensure_builtin_providers

        ensure_builtin_providers(self)

    # ---- lazily-built services --------------------------------------------------------------

    def _service(self, name: str, build: Callable[[], Any]) -> Any:
        svc = self._services.get(name)
        if svc is None:
            with self._services_lock:
                svc = self._services.get(name)
                if svc is None:
                    svc = build()
                    self._services[name] = svc
        return svc

    @property
    def blobs(self) -> BlobStore:
        from ragcentre.blobs import BlobStore

        result: BlobStore = self._service("blobs", lambda: BlobStore(self.settings.blobs_dir))
        return result

    @property
    def secrets(self) -> SecretBox:
        from ragcentre.secrets import SecretBox

        result: SecretBox = self._service("secrets", lambda: SecretBox(self.settings.secret_key))
        return result

    @property
    def providers(self) -> ProviderManager:
        from ragcentre.providers.manager import ProviderManager

        result: ProviderManager = self._service("providers", lambda: ProviderManager(self))
        return result

    @property
    def embeddings(self) -> EmbeddingService:
        from ragcentre.embedding_service import EmbeddingService

        result: EmbeddingService = self._service("embeddings", lambda: EmbeddingService(self))
        return result

    def pipeline_status(self, pipeline_id: str) -> Any:
        from ragcentre.pipeline.status import status

        return status(self, pipeline_id)

    def close_workers(self) -> None:
        if self.workers is not None:
            self.workers.stop(timeout=self.settings.shutdown_grace_seconds)
            self.workers = None

    def start_workers(self, count: int) -> None:
        """Start ingestion worker threads."""
        if self.workers is not None or count <= 0:
            return
        from ragcentre.queue.worker import WorkerPool

        self.workers = WorkerPool(self, count)
        self.workers.start()

    @property
    def worker_count(self) -> int:
        return int(self.workers.size) if self.workers is not None else 0

    # ---- shut-down --------------------------------------------------------------------------

    def close(self) -> None:
        if self.workers is not None:
            self.workers.stop()
            self.workers = None
        for svc in self._services.values():
            close = getattr(svc, "close", None)
            if close is not None:
                close()
        self._services.clear()
        self.db.close()
        self.cache.close()


def open_context(settings: Settings | None = None, **kwargs: Any) -> AppContext:
    ctx = AppContext(settings or Settings.from_env(), **kwargs)
    try:
        ctx.initialise()
    except BaseException:
        ctx.close()
        raise
    return ctx
