from __future__ import annotations

import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from ..inputs import CsvPreview, CsvPreviewStore
from ..warnings import classify_warnings
from .auth import (
    AccessIdentity,
    AccessVerifier,
    CloudWebSettings,
    CloudflareAccessVerifier,
    CsrfSigner,
)
from .db import CloudDatabase
from .delivery import DeliveryRepository, DeliveryUnavailable, TemporaryDeliveryService
from .models import (
    CSV_PROFILE_OUTPUTS,
    CloudJobRequest,
    CloudOutput,
    CloudProfile,
    DeliveryState,
    ProcessingState,
    display_status,
)
from .storage import R2DeliveryStorage
from .web_repository import CloudWebRepository, JobView, format_timestamp


OUTPUT_LABELS = {
    "source": "Native source master",
    "ableton": "Ableton WAV, 32-bit float",
    "wav24": "Standard WAV, 24-bit",
    "listen": "MP3 listening copy",
    "package": "Archive package",
}

SIDECAR_LABELS = {
    "source.info.json": "Source metadata from YouTube",
    "archive.json": "Archive manifest",
    "SHA256SUMS": "Checksums",
    "ingest.log": "Acquisition log",
}

REQUESTED_LABELS = {
    "ableton": "Ableton WAV",
    "wav24": "Standard WAV",
    "listen": "MP3",
    "package": "Archive package",
}


WARNING_CATEGORY_LABELS = {
    "javascript_runtime": "JavaScript runtime",
    "challenge": "signature challenge",
    "po_token": "PO token",
    "authentication": "authentication",
    "format": "format availability",
    "region": "region restriction",
    "throttling": "rate limiting",
    "transport": "network transport",
    "extraction": "page extraction",
    "other": "other",
}


def _warning_view(summary: str | None) -> dict[str, object] | None:
    """Summarize the recorded quality warnings, keeping the detail available.

    Tool output is long and repetitive; pasting it whole says less than naming the
    conditions it describes. The full text stays behind a disclosure so a diagnosis
    loses nothing.
    """

    text = (summary or "").strip()
    if not text:
        return None
    warnings = classify_warnings("", text.replace("; ", "\n"))
    categories: list[str] = []
    for warning in warnings:
        label = WARNING_CATEGORY_LABELS.get(warning.category, warning.category)
        if label not in categories:
            categories.append(label)
    return {
        "count": len(warnings) or 1,
        "categories": categories or [WARNING_CATEGORY_LABELS["other"]],
        "detail": text,
    }


def _output_label(role: str, filename: str) -> str:
    """Name a published file by what it is.

    Every preservation sidecar is delivered under the source role, so the role alone
    would label a checksum file as the audio master.
    """

    if role != "source":
        return OUTPUT_LABELS.get(role, role)
    if filename in SIDECAR_LABELS:
        return SIDECAR_LABELS[filename]
    if filename.startswith("source-thumbnail"):
        return "Source artwork"
    return OUTPUT_LABELS["source"]


@dataclass(frozen=True)
class WebDependencies:
    settings: CloudWebSettings
    database: CloudDatabase
    delivery: TemporaryDeliveryService
    verifier: AccessVerifier


class IdentityRateLimiter:
    """Small in-process limiter suitable for the single Cloud v0.1 web service."""

    def __init__(self, *, limit: int = 30, window_seconds: int = 3600) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("Rate limit values must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, identity: AccessIdentity) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events.setdefault(identity.subject, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                raise PermissionError("Submission rate limit exceeded")
            events.append(now)


def build_web_dependencies(settings: CloudWebSettings | None = None) -> WebDependencies:
    resolved = settings or CloudWebSettings.from_env()
    database = CloudDatabase(resolved.database_url)
    storage = R2DeliveryStorage.from_settings(resolved)  # type: ignore[arg-type]
    delivery = TemporaryDeliveryService(
        repository=DeliveryRepository(database),
        storage=storage,
        retention_hours=resolved.retention_hours,
    )
    return WebDependencies(
        settings=resolved,
        database=database,
        delivery=delivery,
        verifier=CloudflareAccessVerifier(resolved),
    )


def create_cloud_app(
    dependencies: WebDependencies | None = None,
    *,
    rate_limiter: IdentityRateLimiter | None = None,
    csv_staging_root: Path | None = None,
) -> FastAPI:
    deps = dependencies or build_web_dependencies()
    database = deps.database
    repository = CloudWebRepository(database)
    csrf = CsrfSigner(deps.settings.csrf_secret)
    limiter = rate_limiter or IdentityRateLimiter()
    previews = CsvPreviewStore(
        csv_staging_root or Path(tempfile.gettempdir()) / "audio-archive-csv",
        deps.settings.max_csv_bytes,
    )
    templates = Jinja2Templates(
        directory=str(Path(__file__).resolve().parent / "web" / "templates")
    )

    app = FastAPI(title="Audio Archive Cloud", docs_url=None, redoc_url=None)
    app.state.database = database
    app.state.delivery = deps.delivery
    app.state.web_repository = repository

    @app.middleware("http")
    async def authenticate(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path == "/healthz":
            response = await call_next(request)
        else:
            assertion = request.headers.get("Cf-Access-Jwt-Assertion", "")
            try:
                request.state.identity = deps.verifier.verify(assertion)
            except PermissionError:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Authenticated private access is required"},
                    headers={"Cache-Control": "no-store"},
                )
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        identity = _identity(request)
        jobs = [_job_payload(row) for row in repository.list_jobs(limit=100)]
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "identity": identity,
                "csrf_token": csrf.issue(identity),
                "jobs": jobs,
                "counts": repository.summarize_counts(),
                "paused": database.queue_paused(),
            },
        )

    @app.post("/jobs")
    async def create_job(
        request: Request,
        csrf_token: str = Form(...),
        artist: str | None = Form(default=None),
        title: str | None = Form(default=None),
        version: str | None = Form(default=None),
        url: str | None = Form(default=None),
        profile: str = Form(default="ableton"),
        outputs: list[str] = Form(default=[]),
        output_choice: str = Form(default=""),
    ):
        identity = _identity(request)
        _verify_csrf(csrf, csrf_token, identity)
        try:
            limiter.check(identity)
            # Unchecking every box posts no outputs at all, so the form sends a marker:
            # with it, an empty set means source-only; without it, the preset still applies.
            selected = (
                frozenset(CloudOutput(value) for value in outputs)
                if output_choice == "explicit"
                else None
            )
            cloud_profile = CloudProfile(profile)
            origin = "url" if url and url.strip() else "manual"
            job_id = database.create_job(
                CloudJobRequest(
                    artist=_optional(artist),
                    title=_optional(title),
                    version=_optional(version),
                    url=_optional(url),
                    profile=cloud_profile,
                    origin=origin,
                    outputs=selected,
                )
            )
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.post("/queue/{action}")
    async def control_queue(request: Request, action: str, csrf_token: str = Form(...)):
        _verify_csrf(csrf, csrf_token, _identity(request))
        if action not in {"pause", "resume"}:
            raise HTTPException(status_code=404, detail="Unknown queue action")
        # Pausing stops the next claim; a job already running is never interrupted.
        database.set_queue_paused(action == "pause")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/csv/preview", response_class=HTMLResponse)
    async def preview_csv_upload(
        request: Request,
        csrf_token: str = Form(...),
        file: UploadFile = File(...),
    ):
        identity = _identity(request)
        _verify_csrf(csrf, csrf_token, identity)
        content = await file.read(deps.settings.max_csv_bytes + 1)
        try:
            token, preview = previews.create(file.filename or "import.csv", content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="csv.html",
            context={
                "identity": identity,
                "csrf_token": csrf.issue(identity),
                "token": token,
                "preview": _preview_payload(preview),
            },
        )

    @app.post("/csv/import/{token}")
    async def import_csv(request: Request, token: str, csrf_token: str = Form(...)):
        identity = _identity(request)
        _verify_csrf(csrf, csrf_token, identity)
        try:
            limiter.check(identity)
            preview = previews.consume(token)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        import_id = database.create_csv_import(
            filename=preview.filename,
            file_sha256=preview.file_sha256,
            accepted_rows=len(preview.accepted),
            rejected_rows=len(preview.rejected),
            duplicate_rows=len(preview.duplicate_rows),
        )
        for row in preview.accepted:
            database.create_job(
                CloudJobRequest(
                    artist=row.artist,
                    title=row.title,
                    version=row.version,
                    url=row.url,
                    origin="csv",
                    import_id=import_id,
                    import_row=row.import_row,
                    outputs=CSV_PROFILE_OUTPUTS[row.profile],
                )
            )
        return RedirectResponse(url="/", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: int):
        identity = _identity(request)
        try:
            view = repository.get_job_view(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="job.html",
            context={
                "identity": identity,
                "csrf_token": csrf.issue(identity),
                "view": _view_payload(view),
            },
        )

    @app.post("/jobs/{job_id}/approve/{video_id}")
    async def approve_candidate(
        request: Request,
        job_id: int,
        video_id: str,
        csrf_token: str = Form(...),
    ):
        _verify_csrf(csrf, csrf_token, _identity(request))
        try:
            repository.approve_candidate(job_id, video_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/replace-source")
    async def replace_source(
        request: Request,
        job_id: int,
        url: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _verify_csrf(csrf, csrf_token, _identity(request))
        try:
            repository.replace_source_url(job_id, url)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/not-found")
    async def mark_not_found(
        request: Request,
        job_id: int,
        csrf_token: str = Form(...),
    ):
        _verify_csrf(csrf, csrf_token, _identity(request))
        try:
            repository.mark_not_found(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, job_id: int, csrf_token: str = Form(...)):
        _verify_csrf(csrf, csrf_token, _identity(request))
        try:
            repository.cancel_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/delete-files")
    async def delete_files(request: Request, job_id: int, csrf_token: str = Form(...)):
        _verify_csrf(csrf, csrf_token, _identity(request))
        try:
            deps.delivery.repository.request_early_deletion(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/retry")
    async def retry_job(
        request: Request,
        job_id: int,
        csrf_token: str = Form(...),
    ):
        _verify_csrf(csrf, csrf_token, _identity(request))
        try:
            repository.retry_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}/outputs/{output_id}/download")
    async def download_output(job_id: int, output_id: int):
        try:
            signed_url = deps.delivery.download_url(job_id=job_id, output_id=output_id)
        except DeliveryUnavailable as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        return RedirectResponse(url=signed_url, status_code=302)

    @app.get("/api/jobs")
    async def api_jobs():
        return {"jobs": [_job_payload(row) for row in repository.list_jobs(limit=100)]}

    @app.get("/api/jobs/{job_id}")
    async def api_job(job_id: int):
        try:
            return _view_payload(repository.get_job_view(job_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


def _identity(request: Request) -> AccessIdentity:
    identity = getattr(request.state, "identity", None)
    if not isinstance(identity, AccessIdentity):
        raise HTTPException(status_code=403, detail="Authenticated identity is required")
    return identity


def _verify_csrf(signer: CsrfSigner, token: str, identity: AccessIdentity) -> None:
    try:
        signer.verify(token, identity)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Invalid or expired form token") from exc


def _preview_payload(preview: CsvPreview) -> dict[str, object]:
    return {
        "filename": preview.filename,
        "file_sha256": preview.file_sha256,
        "accepted": [
            {
                "row": row.import_row,
                "artist": row.artist,
                "title": row.title,
                "version": row.version,
                "url": row.url,
                "files": sorted(output.value for output in CSV_PROFILE_OUTPUTS[row.profile])
                or ["source only"],
            }
            for row in preview.accepted
        ],
        "rejected": [
            {"row": rejected.row_number, "message": rejected.message}
            for rejected in preview.rejected
        ],
        "duplicate_rows": list(preview.duplicate_rows),
    }


def _optional(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _job_payload(row: dict[str, object]) -> dict[str, object]:
    processing = ProcessingState(str(row["processing_state"]))
    delivery = DeliveryState(str(row["delivery_state"]))
    return {
        "id": int(row["id"]),
        "status": display_status(processing, delivery),
        "processing_state": processing.value,
        "delivery_state": delivery.value,
        "artist": row["requested_artist"],
        "title": row["requested_title"],
        "version": row["requested_version"],
        "requested_url": row["requested_url"],
        "profile": row["profile"],
        "requested_outputs": list(row["requested_outputs"] or ()),
        "requested_labels": [
            REQUESTED_LABELS.get(str(value), str(value))
            for value in sorted(row["requested_outputs"] or ())
        ],
        "source_title": row["source_title"],
        "source_creator": row["source_creator"],
        "quality_status": row["quality_status"],
        "warning_summary": row["warning_summary"],
        "warnings": _warning_view(row["warning_summary"]),
        "error_stage": row["error_stage"],
        "error_class": row["error_class"],
        "error_summary": row["error_summary"],
        "retry_count": int(row["retry_count"]),
        "access_retry_count": int(row["access_retry_count"]),
        "retry_at": format_timestamp(row["retry_not_before_utc"]),
        "created_at": format_timestamp(row["created_at_utc"]),
        "updated_at": format_timestamp(row["updated_at_utc"]),
        "expires_at": format_timestamp(row["expires_at_utc"]),
    }


def _view_payload(view: JobView) -> dict[str, object]:
    job = _job_payload(view.job)
    outputs = []
    for row in view.outputs:
        deleted = row["deleted_at_utc"] is not None
        outputs.append(
            {
                "id": int(row["id"]),
                "role": row["role"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "size_bytes": int(row["size_bytes"]),
                "label": _output_label(str(row["role"]), str(row["filename"])),
                "sha256": row["sha256"],
                "expires_at": format_timestamp(row["expires_at_utc"]),
                "deleted": deleted,
            }
        )
    return {
        "job": job,
        "candidates": list(view.candidates),
        "events": [
            {
                **row,
                "occurred_at_utc": format_timestamp(row.get("occurred_at_utc")),
            }
            for row in view.events
        ],
        "outputs": outputs,
    }
