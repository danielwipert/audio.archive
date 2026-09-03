from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import psycopg
import uvicorn

from ..acquisition import BGUTIL_PROVIDER_DIRECTORY
from ..config import AppConfig, discover_project_root, load_config
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


def bgutil_server_command(server_home: Path, deno: str, port: int) -> tuple[str, ...]:
    """The BgUtils server invocation, matching the permissions upstream's image grants."""

    modules = server_home / "node_modules"
    return (
        deno,
        "run",
        "--allow-env",
        "--allow-net",
        f"--allow-ffi={modules}",
        f"--allow-read={modules}",
        str(server_home / "src" / "main.ts"),
        "--port",
        str(port),
    )


class BgUtilServer:
    """Run the BgUtils PO token server beside the worker for the worker's lifetime.

    Token generation is the step a slow egress path breaks: the script provider fetches
    YouTube's homepage per request and can exceed the provider's fixed timeout. This
    server keeps one warm session instead, and the provider hands it the page yt-dlp
    already fetched.
    """

    def __init__(self, *, server_home: Path, deno: str, port: int = 4416):
        self.server_home = server_home
        self.deno = deno
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, *, timeout_seconds: float = 60.0) -> bool:
        """Start the server and wait for it to answer. False if it never does."""

        entry = self.server_home / "src" / "main.ts"
        if not entry.is_file():
            LOGGER.error("BgUtils server source is missing at %s", entry)
            return False
        self.process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            bgutil_server_command(self.server_home, self.deno, self.port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                LOGGER.error(
                    "BgUtils server exited during startup with code %s", self.process.returncode
                )
                return False
            if self._ping():
                LOGGER.info("BgUtils PO token server is ready on %s", self.base_url)
                return True
            time.sleep(1)
        LOGGER.error("BgUtils server did not answer %s/ping before the startup timeout", self.base_url)
        self.stop()
        return False

    def _ping(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/ping", timeout=2) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError):
            return False

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        finally:
            self.process = None


def _acquisition_config(settings: CloudSettings) -> tuple[AppConfig, BgUtilServer | None]:
    """Load the media configuration, starting the token server when it is wanted.

    A server that will not start is reported and the worker continues on the script
    provider: acquisition still succeeds, and its own warning classification records
    that a token problem may have limited the formats.
    """

    config = load_config()
    if os.getenv("AUDIO_ARCHIVE_POT_PROVIDER", "http").strip().casefold() != "http":
        return config, None
    server = BgUtilServer(
        server_home=config.tools_directory / BGUTIL_PROVIDER_DIRECTORY / "server",
        deno=os.getenv("AUDIO_ARCHIVE_DENO", "deno"),
        port=int(os.getenv("AUDIO_ARCHIVE_POT_PORT", "4416")),
    )
    if not server.start():
        LOGGER.warning(
            "Falling back to the BgUtils script provider; acquisitions may report "
            "token warnings and a lower quality status"
        )
        return config, None
    return replace(config, pot_provider="http", pot_http_base_url=server.base_url), server


def _worker_runner(settings: CloudSettings) -> CommandRunner:
    runner: CommandRunner = SubprocessRunner(
        timeout_seconds=settings.subprocess_timeout_seconds
    )
    proxy_url = os.getenv("AUDIO_ARCHIVE_YTDLP_PROXY", "").strip()
    if proxy_url:
        LOGGER.info("YouTube proxy routing is enabled for worker yt-dlp calls")
        runner = YtDlpProxyRunner(runner, proxy_url)
    return runner


def _build_worker(
    settings: CloudSettings,
) -> tuple[CloudSequentialWorker, TemporaryDeliveryService, BgUtilServer | None]:
    database = CloudDatabase(settings.database_url)
    _wait_for_schema(database)
    storage = R2DeliveryStorage.from_settings(settings)
    delivery = TemporaryDeliveryService(
        repository=DeliveryRepository(database),
        storage=storage,
        retention_hours=settings.retention_hours,
    )
    base_config, token_server = _acquisition_config(settings)
    processor = CloudJobProcessor(
        database=database,
        settings=settings,
        base_config=base_config,
        runner=_worker_runner(settings),
        delivery=delivery,
    )
    worker = CloudSequentialWorker(
        database=database,
        settings=settings,
        processor=processor,
    )
    return worker, delivery, token_server


def _run_worker() -> int:
    settings = CloudSettings.from_env()
    worker, delivery, token_server = _build_worker(settings)
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

    if token_server is not None:
        token_server.stop()
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
