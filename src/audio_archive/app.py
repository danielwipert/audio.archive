from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import threading

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .config import AppConfig, load_config
from .db import ArchiveDatabase
from .inputs import CsvPreview, CsvPreviewStore, attach_import_id, normalize_request
from .models import JobState
from .source_resolution import (
    approve_candidate,
    list_resolution_candidates,
    mark_not_found,
    replace_source_url,
)
from .tooling import CommandRunner, SubprocessRunner
from .worker import SequentialWorker


class QueueController:
    """Keeps blocking queue work off the local HTTP request thread."""

    def __init__(self, worker: SequentialWorker):
        self.worker = worker
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active = False
        self._last_error: str | None = None
        self._thread = threading.Thread(target=self._loop, name="audio-archive-worker", daemon=True)

    def recover(self) -> None:
        self.worker.recover_startup()

    def launch(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()
        self.wake()

    def wake(self) -> None:
        """Wake queued work without overriding an explicit pause."""

        if not self.worker.paused:
            self._wake.set()

    def start(self) -> None:
        self.worker.resume()
        self._wake.set()

    def pause_after_current(self) -> None:
        self.worker.request_pause()

    def resume(self) -> None:
        self.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._lock:
                self._active = True
                self._last_error = None
            try:
                self.worker.run_until_idle()
            except Exception as exc:  # noqa: BLE001 - keep the local UI alive for recovery
                with self._lock:
                    self._last_error = str(exc)
            finally:
                with self._lock:
                    self._active = False


def _preview_payload(token: str, preview: CsvPreview) -> dict[str, object]:
    return {
        "token": token,
        "filename": preview.filename,
        "file_sha256": preview.file_sha256,
        "accepted": [
            {
                "row": request.import_row,
                "artist": request.artist,
                "title": request.title,
                "version": request.version,
                "url": request.url,
                "profile": request.profile,
            }
            for request in preview.accepted
        ],
        "rejected": [
            {"row": rejected.row_number, "message": rejected.message}
            for rejected in preview.rejected
        ],
        "duplicate_rows": list(preview.duplicate_rows),
    }


def _candidate_payload(row) -> dict[str, object]:
    import json

    return {
        "position": row["position"],
        "video_id": row["video_id"],
        "url": row["url"],
        "title": row["title"],
        "channel": row["channel"],
        "duration_seconds": row["duration_seconds"],
        "thumbnail_url": row["thumbnail_url"],
        "score": row["score"],
        "reasons": json.loads(row["reasons_json"]),
        "warnings": json.loads(row["warnings_json"]),
        "disqualified": bool(row["disqualified"]),
    }


def _job_payload(database: ArchiveDatabase, row) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": row["id"],
        "state": row["state"],
        "origin": row["origin"],
        "profile": row["profile"],
        "artist": row["requested_artist"],
        "title": row["requested_title"],
        "version": row["requested_version"],
        "requested_url": row["requested_url"],
        "source_id": row["source_id"],
        "source_url": row["source_url"],
        "source_title": row["source_title"],
        "source_creator": row["source_creator"],
        "resolution_method": row["resolution_method"],
        "selected_score": row["selected_score"],
        "runner_up_score": row["runner_up_score"],
        "progress_percent": row["progress_percent"],
        "quality_status": row["quality_status"],
        "warning_summary": row["warning_summary"],
        "error_stage": row["error_stage"],
        "error_summary": row["error_summary"],
        "retry_count": row["retry_count"],
    }
    if row["state"] == JobState.NEEDS_REVIEW.value:
        payload["candidates"] = [
            _candidate_payload(candidate)
            for candidate in list_resolution_candidates(database, int(row["id"]))
        ]
    if row["source_extractor"] and row["source_id"]:
        item = database.find_archive_item(str(row["source_extractor"]), str(row["source_id"]))
        if item is not None:
            item_directory = Path(item["item_directory"])
            payload["item_directory"] = str(item_directory)
            assets = database.list_assets(str(item["archive_id"]))
            ableton_paths = [
                str(item_directory / asset["relative_path"])
                for asset in assets
                if asset["role"] == "ableton"
            ]
            payload["ableton_paths"] = ableton_paths
    return payload


def create_app(
    config: AppConfig | None = None,
    runner: CommandRunner | None = None,
    *,
    start_worker_thread: bool = True,
) -> FastAPI:
    resolved_config = config or load_config()
    database = ArchiveDatabase(resolved_config.database_path)
    database.initialize()
    worker = SequentialWorker(database, resolved_config, runner or SubprocessRunner())
    controller = QueueController(worker)
    previews = CsvPreviewStore(
        resolved_config.temp_directory / "csv-previews",
        resolved_config.max_csv_bytes,
    )
    package_root = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(package_root / "web" / "templates"))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        controller.recover()
        if start_worker_thread:
            controller.launch()
        yield
        controller.shutdown()

    app = FastAPI(title="Audio Archive", lifespan=lifespan)
    app.state.database = database
    app.state.config = resolved_config
    app.state.controller = controller
    app.state.previews = previews
    app.mount("/static", StaticFiles(directory=str(package_root / "web" / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "poll_interval_ms": max(500, int(resolved_config.poll_interval_seconds * 1000)),
            },
        )

    @app.get("/api/status")
    async def status():
        jobs = [_job_payload(database, row) for row in database.list_jobs()]
        return {
            "queue": {
                "active": controller.active,
                "paused": worker.paused,
                "last_error": controller.last_error,
            },
            "jobs": jobs,
            "needs_review": sum(1 for job in jobs if job["state"] == "needs_review"),
        }

    @app.post("/api/jobs")
    async def create_job(
        artist: str | None = Form(default=None),
        title: str | None = Form(default=None),
        version: str | None = Form(default=None),
        url: str | None = Form(default=None),
        profile: str = Form(default="ableton"),
        start_queue: bool = Form(default=True),
    ):
        try:
            request = normalize_request(
                artist=artist,
                title=title,
                version=version,
                url=url,
                profile=profile,
                origin="manual",
            )
            job_id = database.create_job(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if start_queue:
            controller.wake()
        return {"job_id": job_id, "state": database.get_job(job_id)["state"]}

    @app.post("/api/csv/preview")
    async def preview_import(file: UploadFile = File(...)):
        content = await file.read(resolved_config.max_csv_bytes + 1)
        try:
            token, preview = previews.create(file.filename or "import.csv", content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _preview_payload(token, preview)

    @app.post("/api/csv/import/{token}")
    async def import_csv(token: str, start_queue: bool = Form(default=True)):
        try:
            preview = previews.consume(token)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        import_id = database.create_csv_import(
            filename=preview.filename,
            file_sha256=preview.file_sha256,
            accepted_rows=len(preview.accepted),
            rejected_rows=len(preview.rejected),
            duplicate_rows=len(preview.duplicate_rows),
        )
        job_ids = [
            database.create_job(attach_import_id(request, import_id))
            for request in preview.accepted
        ]
        if start_queue and job_ids:
            controller.start()
        return {"import_id": import_id, "job_ids": job_ids}

    @app.post("/api/jobs/{job_id}/approve/{video_id}")
    async def approve(job_id: int, video_id: str):
        try:
            approve_candidate(database, job_id, video_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        controller.wake()
        return {"job_id": job_id, "state": database.get_job(job_id)["state"]}

    @app.post("/api/jobs/{job_id}/replace-source")
    async def replace_source(job_id: int, url: str = Form(...)):
        try:
            replace_source_url(database, job_id, url)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        controller.wake()
        return {"job_id": job_id, "state": database.get_job(job_id)["state"]}

    @app.post("/api/jobs/{job_id}/not-found")
    async def not_found(job_id: int):
        try:
            mark_not_found(database, job_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job_id": job_id, "state": database.get_job(job_id)["state"]}

    @app.post("/api/jobs/{job_id}/retry")
    async def retry(job_id: int):
        try:
            state = database.retry_job(job_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        controller.wake()
        return {"job_id": job_id, "state": state.value}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: int):
        try:
            database.transition_job(job_id, JobState.CANCELLED, message="Cancelled by user")
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job_id": job_id, "state": JobState.CANCELLED.value}

    @app.post("/api/jobs/{job_id}/open-folder")
    async def open_folder(job_id: int):
        try:
            job = database.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not job["source_extractor"] or not job["source_id"]:
            raise HTTPException(status_code=400, detail="Job has no archived source")
        item = database.find_archive_item(str(job["source_extractor"]), str(job["source_id"]))
        if item is None:
            raise HTTPException(status_code=404, detail="Archive item is not recorded")
        path = Path(item["item_directory"])
        if not path.is_dir():
            raise HTTPException(status_code=404, detail="Archive item folder is missing")
        if os.name != "nt":
            return {"path": str(path), "opened": False}
        os.startfile(str(path))  # type: ignore[attr-defined]
        return {"path": str(path), "opened": True}

    @app.post("/api/queue/start")
    async def start_queue():
        controller.start()
        return {"active": controller.active, "paused": worker.paused}

    @app.post("/api/queue/pause")
    async def pause_queue():
        controller.pause_after_current()
        return {"active": controller.active, "paused": worker.paused}

    @app.post("/api/queue/resume")
    async def resume_queue():
        controller.resume()
        return {"active": controller.active, "paused": worker.paused}

    return app
