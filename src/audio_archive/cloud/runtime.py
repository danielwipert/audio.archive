from __future__ import annotations

import os
import signal
from pathlib import Path
from threading import Event

import uvicorn

from ..config import discover_project_root, load_config
from ..tooling import SubprocessRunner
from .app import build_web_dependencies, create_cloud_app
from .config import CloudSettings
from .db import CloudDatabase
from .delivery import DeliveryRepository, TemporaryDeliveryService
from .pipeline import CloudJobProcessor
from .storage import R2DeliveryStorage
from .worker import CloudSequentialWorker


def migrate_main() -> None:
    root = discover_project_root()
    database_url = _required("DATABASE_URL")
    database = CloudDatabase(database_url)
    applied = database.apply_migrations(root / "migrations")
    print(f"Audio Archive cloud migrations applied: {applied or 'none'}", flush=True)


def web_main() -> None:
    root = discover_project_root()
    dependencies = build_web_dependencies()
    applied = dependencies.database.apply_migrations(root / "migrations")
    if applied:
        print(f"Applied database migrations: {applied}", flush=True)
    app = create_cloud_app(dependencies)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=_positive_int("PORT", 8000),
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=True,
    )


def worker_main() -> None:
    root = discover_project_root()
    settings = CloudSettings.from_env()
    database = CloudDatabase(settings.database_url)
    applied = database.apply_migrations(root / "migrations")
    if applied:
        print(f"Applied database migrations: {applied}", flush=True)

    runner = SubprocessRunner()
    base_config = load_config(root)
    storage = R2DeliveryStorage.from_settings(settings)
    delivery = TemporaryDeliveryService(
        repository=DeliveryRepository(database),
        storage=storage,
        retention_hours=settings.retention_hours,
    )
    processor = CloudJobProcessor(
        database=database,
        settings=settings,
        base_config=base_config,
        runner=runner,
        delivery=delivery,
    )
    worker = CloudSequentialWorker(
        database=database,
        settings=settings,
        processor=processor,
        lease_seconds=_positive_int("AUDIO_ARCHIVE_WORKER_LEASE_SECONDS", 300),
        heartbeat_interval_seconds=_positive_float(
            "AUDIO_ARCHIVE_WORKER_HEARTBEAT_SECONDS", 60.0
        ),
    )
    recovered = worker.recover_startup()
    if recovered:
        print(f"Recovered abandoned cloud jobs: {list(recovered)}", flush=True)

    stop = Event()
    _install_stop_handlers(stop)
    poll_seconds = _positive_float("AUDIO_ARCHIVE_WORKER_POLL_SECONDS", 2.0)
    print(
        f"Audio Archive worker {settings.worker_id} ready "
        f"({settings.worker_network_class.value})",
        flush=True,
    )
    while not stop.is_set():
        result = worker.run_next()
        if result is None:
            stop.wait(poll_seconds)
            continue
        if result.error:
            print(
                f"Job {result.job_id} ended in {result.state.value}: {result.error}",
                flush=True,
            )
        else:
            print(f"Job {result.job_id} -> {result.state.value}", flush=True)


def cleanup_main() -> None:
    root = discover_project_root()
    settings = CloudSettings.from_env()
    database = CloudDatabase(settings.database_url)
    applied = database.apply_migrations(root / "migrations")
    if applied:
        print(f"Applied database migrations: {applied}", flush=True)
    storage = R2DeliveryStorage.from_settings(settings)
    delivery = TemporaryDeliveryService(
        repository=DeliveryRepository(database),
        storage=storage,
        retention_hours=settings.retention_hours,
    )

    deleted = 0
    for _ in range(100):
        count = delivery.cleanup_expired(limit=100)
        deleted += count
        if count < 100:
            break
    print(f"Temporary delivery cleanup reconciled {deleted} object(s)", flush=True)


def _install_stop_handlers(stop: Event) -> None:
    def handle_stop(signum, frame):  # type: ignore[no-untyped-def]
        del signum, frame
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        signal_value = getattr(signal, name, None)
        if signal_value is not None:
            signal.signal(signal_value, handle_stop)


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value
