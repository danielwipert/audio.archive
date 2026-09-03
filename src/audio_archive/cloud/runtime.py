from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
from pathlib import Path

import psycopg
import uvicorn

from ..config import discover_project_root, load_config
from ..tooling import CommandRunner, SubprocessRunner
from .app import build_web_dependencies, create_cloud_app
from .config import CloudSettings
from .db import CloudDatabase
from .delivery import DeliveryRepository, TemporaryDeliveryService
from .pipeline import CloudJobProcessor
from .proxy import YtDlpProxyRunner
from .storage import R2DeliveryStorage
from .worker import CloudSequentialWorker
from .workspace import sweep_stale_workspaces

LOGGER = logging.getLogger("audio_archive.cloud.runtime")


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("AUDIO_ARCHIVE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _migrations_dir() -> Path:
    return discover_project_root() / "migrations"


def _run_web() -> int:
    dependencies = build_web_dependencies()
    applied = dependencies.database.apply_migrations(_migrations_dir())
    if applied:
        LOGGER.info("Applied PostgreSQL migrations: %s", applied)

    port = int(os.getenv("PORT", "8000"))
    app = create_cloud_app(dependencies)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=True,
    )
    return 0


def expected_migration_versions(migrations_dir: Path) -> set[int]:
    """Versions this deployment expects, taken from the migration files it ships with."""

    return {
        int(path.name.split("_", 1)[0])
        for path in migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")
    }


def _wait_for_schema(database: CloudDatabase, *, timeout_seconds: int = 180) -> None:
    """Block until the web service has applied every migration this worker ships with.

    Waiting on the jobs table alone was enough for one migration. A worker that starts
    against a partly migrated database would fail every claim until the web service
    caught up, so the wait covers the whole expected set.
    """

    expected = expected_migration_versions(_migrations_dir())
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with database.connect() as connection:
                applied = {
                    int(row["version"])
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    ).fetchall()
                }
            if expected <= applied:
                return
            LOGGER.info(
                "Waiting for migrations %s to be applied by the web service",
                sorted(expected - applied),
            )
        except (psycopg.Error, OSError) as exc:
            last_error = exc
        time.sleep(2)
    raise RuntimeError("Cloud database schema was not ready before worker startup timeout") from last_error


def _worker_runner(settings: CloudSettings) -> CommandRunner:
    runner: CommandRunner = SubprocessRunner(
        timeout_seconds=settings.subprocess_timeout_seconds
    )
    proxy_url = os.getenv("AUDIO_ARCHIVE_YTDLP_PROXY", "").strip()
    if proxy_url:
        LOGGER.info("YouTube proxy routing is enabled for worker yt-dlp calls")
        runner = YtDlpProxyRunner(runner, proxy_url)
    return runner


def _build_worker(settings: CloudSettings) -> tuple[CloudSequentialWorker, TemporaryDeliveryService]:
    database = CloudDatabase(settings.database_url)
    _wait_for_schema(database)
    storage = R2DeliveryStorage.from_settings(settings)
    delivery = TemporaryDeliveryService(
        repository=DeliveryRepository(database),
        storage=storage,
        retention_hours=settings.retention_hours,
    )
    processor = CloudJobProcessor(
        database=database,
        settings=settings,
        base_config=load_config(),
        runner=_worker_runner(settings),
        delivery=delivery,
    )
    worker = CloudSequentialWorker(
        database=database,
        settings=settings,
        processor=processor,
    )
    return worker, delivery


def _run_worker() -> int:
    settings = CloudSettings.from_env()
    worker, delivery = _build_worker(settings)
    recovered = worker.recover_startup()
    if recovered:
        LOGGER.warning("Recovered abandoned cloud jobs: %s", recovered)

    stop = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        LOGGER.info("Received signal %s; stopping after current operation", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    poll_seconds = float(os.getenv("AUDIO_ARCHIVE_WORKER_POLL_SECONDS", "2"))
    cleanup_seconds = float(os.getenv("AUDIO_ARCHIVE_CLEANUP_INTERVAL_SECONDS", "300"))
    if poll_seconds <= 0 or cleanup_seconds <= 0:
        raise ValueError("Worker poll and cleanup intervals must be positive")

    next_cleanup = 0.0
    LOGGER.info("Cloud worker %s is ready", settings.worker_id)
    while not stop.is_set():
        now = time.monotonic()
        if now >= next_cleanup:
            try:
                deleted = delivery.cleanup_expired(limit=100)
                if deleted:
                    LOGGER.info("Deleted %s expired delivery objects", deleted)
            except Exception:  # noqa: BLE001 - cleanup should not take the worker down
                LOGGER.exception("Expired-delivery cleanup failed")
            try:
                swept = sweep_stale_workspaces(
                    settings,
                    is_retainable=worker.database.job_may_run_again,
                    retention_hours=settings.scratch_retention_hours,
                )
                if swept:
                    LOGGER.info("Removed scratch workspaces for jobs %s", list(swept))
            except Exception:  # noqa: BLE001 - cleanup should not take the worker down
                LOGGER.exception("Scratch workspace sweep failed")
            next_cleanup = now + cleanup_seconds

        try:
            result = worker.run_next()
        except Exception:  # noqa: BLE001 - transient infrastructure errors should be retried
            LOGGER.exception("Cloud worker iteration failed")
            stop.wait(min(10.0, poll_seconds))
            continue

        if result is None:
            stop.wait(poll_seconds)
            continue

        if result.error:
            LOGGER.error(
                "Job %s finished in state %s with error: %s",
                result.job_id,
                result.state.value,
                result.error,
            )
        else:
            LOGGER.info("Job %s finished in state %s", result.job_id, result.state.value)

    LOGGER.info("Cloud worker stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="Audio Archive Cloud production runtime")
    parser.add_argument("role", choices=("web", "worker"))
    args = parser.parse_args(argv)
    if args.role == "web":
        return _run_web()
    return _run_worker()


if __name__ == "__main__":
    raise SystemExit(main())
