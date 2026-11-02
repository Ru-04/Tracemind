"""Background worker process: claims queued scans and runs them.

    python -m tracemind.worker

It shares nothing with the API process except PostgreSQL and the data directory. It is
read-only with respect to registered repositories."""

from __future__ import annotations

import logging
import os
import signal
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from tracemind.config import ConfigError, Settings
from tracemind.observability.logging import LOGGER_NAME, configure_logging, log
from tracemind.repos.graphify_runner import GraphifyRunner
from tracemind.repos.scan_service import ProcessResult, ScanService
from tracemind.repos.scan_store import ScanStore
from tracemind.repos.service import RepositoryService
from tracemind.repos.store import RepositoryStore
from tracemind.storage.postgres import PostgresStore

_logger = logging.getLogger(LOGGER_NAME + ".worker")


class ScanWorker:
    def __init__(
        self,
        service: ScanService,
        *,
        worker_id: str | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        self._service = service
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._poll = poll_interval
        self.stop_event = threading.Event()

    def run_once(self) -> ProcessResult | None:
        return self._service.process_next(self.worker_id)

    def run_forever(self) -> None:
        log(_logger, logging.INFO, "worker started", worker_id=self.worker_id)
        while not self.stop_event.is_set():
            try:
                result = self.run_once()
            except Exception as exc:  # noqa: BLE001 - keep the worker alive; log the class only
                log(
                    _logger,
                    logging.ERROR,
                    "worker iteration failed",
                    error_class=type(exc).__name__,
                )
                result = None
            if result is None:
                self.stop_event.wait(self._poll)
        log(_logger, logging.INFO, "worker stopped", worker_id=self.worker_id)


def build_service(settings: Settings) -> ScanService:
    if not settings.database_url:
        raise ConfigError("TRACEMIND_DATABASE_URL is not set")
    if settings.graphify_bin is None:
        raise ConfigError("TRACEMIND_GRAPHIFY_BIN is not set (python -m tools.install_graphify)")
    store = PostgresStore.from_url(settings.database_url)
    repositories = RepositoryService(RepositoryStore(store.engine), settings)
    runner = GraphifyRunner(
        settings.graphify_bin,
        home_dir=settings.data_dir / "graphify-home",
        timeout=settings.scan_timeout_seconds,
    )
    runner.verify()  # refuse to start with a missing, wrong-version or modified Graphify
    return ScanService(
        repositories=repositories,
        scans=ScanStore(store.engine),
        runner=runner,
        settings=settings,
        clock=lambda: datetime.now(UTC),
    )


def main() -> int:
    configure_logging()
    settings = Settings.from_env()
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    worker = ScanWorker(build_service(settings))
    signal.signal(signal.SIGINT, lambda *_: worker.stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: worker.stop_event.set())
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
