from __future__ import annotations

import json
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from zipfile import ZIP_STORED, ZipFile

from ..ableton import AbletonService, PcmResult
from ..acquisition import AcquisitionRequest, AcquisitionResult, AcquisitionService
from ..config import AppConfig
from ..integrity import listed_checksum_paths, verify_sha256sums, write_sha256sums
from ..manifest import write_manifest_atomic
from ..listening import ListeningResult, ListeningService
from ..resolver import decide_resolution
from ..source_resolution import search_youtube_candidates
from ..tooling import CommandRunner
from ..verify import sha256_file
from ..wav24 import Wav24Service
from .config import CloudSettings
from .db import CloudDatabase, LostWorkerClaim
from .delivery import TemporaryDeliveryService
from .execution import CloudExecutionRepository, classify_job_error
from .models import CloudOutput, CloudProfile, ProcessingState, WorkerClaim
from .workspace import CloudWorkspace


class HeartbeatGuard(Protocol):
    def check(self) -> None: ...


CONVERSION_ORDER: tuple[CloudOutput, ...] = (
    CloudOutput.ABLETON,
    CloudOutput.WAV24,
    CloudOutput.LISTEN,
)

CONVERSION_LABELS = {
    CloudOutput.ABLETON: "Ableton 32-bit float WAV",
    CloudOutput.WAV24: "24-bit WAV",
    CloudOutput.LISTEN: "listening MP3",
}


@dataclass(frozen=True)
class Conversions:
    """Whatever this job asked the worker to make from its verified source master."""

    ableton: PcmResult | None = None
    wav24: PcmResult | None = None
    listening: ListeningResult | None = None


def _requested_outputs(job: dict[str, object]) -> frozenset[CloudOutput]:
    return frozenset(CloudOutput(str(value)) for value in (job.get("requested_outputs") or ()))


@dataclass(frozen=True)
class CloudProcessingResult:
    job_id: int
    state: ProcessingState
    output_ids: tuple[int, ...] = ()


class CloudJobProcessor:
    """Run one claimed cloud job through the proven local media services."""

    def __init__(
        self,
        *,
        database: CloudDatabase,
        settings: CloudSettings,
        base_config: AppConfig,
        runner: CommandRunner,
        delivery: TemporaryDeliveryService,
        acquisition_factory: Callable[[AppConfig, CommandRunner], AcquisitionService] = AcquisitionService,
        ableton_factory: Callable[[AppConfig, CommandRunner], AbletonService] = AbletonService,
        wav24_factory: Callable[[AppConfig, CommandRunner], Wav24Service] = Wav24Service,
        listening_factory: Callable[[AppConfig, CommandRunner], ListeningService] = ListeningService,
    ) -> None:
        self.database = database
        self.settings = settings
        self.base_config = base_config
        self.runner = runner
        self.delivery = delivery
        self.execution = CloudExecutionRepository(database)
        self.acquisition_factory = acquisition_factory
        self.ableton_factory = ableton_factory
        self.wav24_factory = wav24_factory
        self.listening_factory = listening_factory

    def process_claim(
        self,
        claim: WorkerClaim,
        *,
        heartbeat: HeartbeatGuard,
    ) -> CloudProcessingResult:
        attempt_id = self.execution.start_attempt(
            job_id=claim.job_id,
            worker_id=claim.worker_id,
            network_class=self.settings.worker_network_class,
        )
        workspace = CloudWorkspace.for_claim(self.settings, claim)
        attempt_closed = False
        try:
            job = self.database.get_job(claim.job_id)
            state = ProcessingState(str(job["processing_state"]))
            if state is ProcessingState.PENDING:
                result = self._resolve_pending(job, heartbeat=heartbeat)
                if result is not None:
                    self.execution.finish_attempt(attempt_id, result=result.state.value)
                    attempt_closed = True
                    return result
                job = self.database.get_job(claim.job_id)
                state = ProcessingState(str(job["processing_state"]))
            if state is not ProcessingState.READY:
                raise ValueError(
                    f"Cloud worker can process only pending or ready jobs, found {state.value}"
                )

            workspace.prepare()
            local_config = workspace.local_config(self.base_config)
            profile = CloudProfile(str(job["profile"]))
            requested = _requested_outputs(job)
            acquisition = self._acquire(
                job,
                profile=profile,
                local_config=local_config,
                heartbeat=heartbeat,
            )
            conversions = self._convert(
                job_id=claim.job_id,
                requested=requested,
                acquisition=acquisition,
                local_config=local_config,
                heartbeat=heartbeat,
            )
            package_path: Path | None = None

            if CloudOutput.PACKAGE in requested:
                self.database.transition_processing(
                    claim.job_id,
                    ProcessingState.PACKAGING,
                    message="Creating verified archive download package",
                )
                package_path = _create_package(
                    acquisition.item_directory,
                    workspace.root / "package",
                    acquisition.video_id,
                )
                heartbeat.check()

            self.database.transition_processing(
                claim.job_id,
                ProcessingState.PUBLISHING,
                message="Publishing verified outputs to temporary private storage",
            )
            heartbeat.check()
            output_ids = self._publish(
                job_id=claim.job_id,
                acquisition=acquisition,
                conversions=conversions,
                package_path=package_path,
                heartbeat=heartbeat,
            )
            final_state = (
                ProcessingState.COMPLETED
                if acquisition.quality_status == "verified_best_available"
                else ProcessingState.COMPLETED_WITH_WARNINGS
            )
            self.database.transition_processing(
                claim.job_id,
                final_state,
                message="Cloud processing completed; temporary downloads are available",
            )
            self.execution.finish_attempt(attempt_id, result=final_state.value)
            attempt_closed = True
            return CloudProcessingResult(claim.job_id, final_state, tuple(output_ids))
        except LostWorkerClaim as exc:
            if not attempt_closed:
                self.execution.finish_attempt(
                    attempt_id,
                    result="lost_claim",
                    error_class=type(exc).__name__,
                    error_summary=str(exc)[:4000],
                )
                attempt_closed = True
            raise
        except Exception as exc:
            self._rollback_unpublished(claim.job_id)
            stage = self._current_stage(claim.job_id)
            try:
                self.execution.fail_job(
                    job_id=claim.job_id,
                    stage=stage,
                    error=exc,
                    retry_policy=self.settings.access_retry_policy,
                )
            finally:
                if not attempt_closed:
                    self.execution.finish_attempt(
                        attempt_id,
                        result="failed",
                        error_class=classify_job_error(stage, exc),
                        error_summary=str(exc)[:4000],
                    )
                    attempt_closed = True
            raise
        finally:
            workspace.cleanup()

    def _resolve_pending(
        self,
        job: dict[str, object],
        *,
        heartbeat: HeartbeatGuard,
    ) -> CloudProcessingResult | None:
        job_id = int(job["id"])
        artist = str(job.get("requested_artist") or "").strip()
        title = str(job.get("requested_title") or "").strip()
        version = str(job.get("requested_version") or "").strip() or None
        if not artist or not title:
            raise ValueError("Pending cloud job has no artist/title resolution request")
        self.database.transition_processing(
            job_id,
            ProcessingState.RESOLVING,
            message=f"Searching up to {self.base_config.candidate_limit} YouTube candidates",
        )
        candidates = search_youtube_candidates(
            self.base_config,
            self.runner,
            artist=artist,
            title=title,
            version=version,
        )
        heartbeat.check()
        decision = decide_resolution(
            artist=artist,
            title=title,
            version=version,
            candidates=list(candidates),
            minimum_score=self.base_config.auto_select_min_score,
            minimum_margin=self.base_config.auto_select_min_margin,
        )
        persisted = self.execution.persist_resolution(job_id=job_id, decision=decision)
        if persisted.state is ProcessingState.READY:
            return None
        return CloudProcessingResult(job_id, persisted.state)

    def _acquire(
        self,
        job: dict[str, object],
        *,
        profile: CloudProfile,
        local_config: AppConfig,
        heartbeat: HeartbeatGuard,
    ) -> AcquisitionResult:
        job_id = int(job["id"])
        source_id = str(job.get("source_id") or "")
        source_url = str(job.get("source_url") or "")
        if str(job.get("source_extractor") or "") != "youtube" or not source_id or not source_url:
            raise ValueError("Ready cloud job does not have a pinned YouTube source")
        self.database.transition_processing(
            job_id,
            ProcessingState.DOWNLOADING,
            message="Native source acquisition started",
        )
        local_profile = "archive" if profile is CloudProfile.SOURCE else "ableton"
        result = self.acquisition_factory(local_config, self.runner).acquire(
            AcquisitionRequest(
                job_id=job_id,
                video_id=source_id,
                url=source_url,
                profile=local_profile,
                artist=_optional_text(job.get("requested_artist")),
                title=_optional_text(job.get("requested_title")),
                version=_optional_text(job.get("requested_version")),
                origin=str(job.get("origin") or "url"),
                import_row=int(job["import_row"]) if job.get("import_row") is not None else None,
                resolution_method=str(job.get("resolution_method") or "exact_url"),
                selected_score=(
                    int(job["selected_score"]) if job.get("selected_score") is not None else None
                ),
                runner_up_score=(
                    int(job["runner_up_score"])
                    if job.get("runner_up_score") is not None
                    else None
                ),
                reviewed_by_user=str(job.get("resolution_method") or "").startswith("manual"),
            )
        )
        _set_cloud_manifest_profile(
            result.item_directory,
            profile.value,
            _requested_outputs(job),
        )
        heartbeat.check()
        self.database.transition_processing(
            job_id,
            ProcessingState.VERIFYING_MASTER,
            message="Native source master passed media and checksum verification",
        )
        self.execution.record_acquisition(
            job_id=job_id,
            source_title=result.source_title,
            source_creator=result.source_creator,
            quality_status=result.quality_status,
            warnings=tuple(warning.message for warning in result.warnings),
        )
        return result

    def _convert(
        self,
        *,
        job_id: int,
        requested: frozenset[CloudOutput],
        acquisition: AcquisitionResult,
        local_config: AppConfig,
        heartbeat: HeartbeatGuard,
    ) -> Conversions:
        """Create every requested derivative from the one verified source master.

        All conversions run inside a single converting stage and are verified together,
        so asking for three formats stays one pass over the master rather than three
        trips through the queue.
        """

        wanted = [output for output in CONVERSION_ORDER if output in requested]
        if not wanted:
            return Conversions()

        self.database.transition_processing(
            job_id,
            ProcessingState.CONVERTING,
            message="Creating "
            + ", ".join(CONVERSION_LABELS[output] for output in wanted)
            + " from the verified source master",
        )
        conversions = Conversions()
        for output in wanted:
            if output is CloudOutput.ABLETON:
                conversions = replace(
                    conversions,
                    ableton=self.ableton_factory(local_config, self.runner).create(
                        acquisition.item_directory, job_id=job_id
                    ),
                )
            elif output is CloudOutput.WAV24:
                conversions = replace(
                    conversions,
                    wav24=self.wav24_factory(local_config, self.runner).create(
                        acquisition.item_directory, job_id=job_id
                    ),
                )
            else:
                conversions = replace(
                    conversions,
                    listening=self.listening_factory(local_config, self.runner).create(
                        acquisition.item_directory, job_id=job_id
                    ),
                )
            heartbeat.check()

        self.database.transition_processing(
            job_id,
            ProcessingState.VERIFYING_OUTPUT,
            message="Every requested output passed media and checksum verification",
        )
        return conversions

    def _publish(
        self,
        *,
        job_id: int,
        acquisition: AcquisitionResult,
        conversions: Conversions,
        package_path: Path | None,
        heartbeat: HeartbeatGuard,
    ) -> list[int]:
        integrity = verify_sha256sums(acquisition.item_directory)
        if not integrity.valid:
            raise ValueError(
                "Ephemeral archive item failed integrity verification before publication: "
                + "; ".join(integrity.errors)
            )
        published_at = datetime.now(UTC)
        expires_at = self.delivery.default_expiry(published_at)
        output_ids: list[int] = []

        for path, filename in _source_publication_files(acquisition):
            heartbeat.check()
            output_ids.append(
                self.delivery.publish_file(
                    job_id=job_id,
                    role="source",
                    path=path,
                    filename=filename,
                    content_type=_content_type(path),
                    expected_sha256=sha256_file(path),
                    published_at_utc=published_at,
                    expires_at_utc=expires_at,
                    media_properties=(
                        {
                            "audio_codec": acquisition.probe.audio.codec,
                            "sample_rate_hz": acquisition.probe.audio.sample_rate_hz,
                            "channels": acquisition.probe.audio.channels,
                            "duration_seconds": acquisition.probe.duration_seconds,
                            "quality_status": acquisition.quality_status,
                        }
                        if path == acquisition.master_path
                        else {"sidecar": True}
                    ),
                )
            )

        for pcm in (conversions.ableton, conversions.wav24):
            if pcm is None:
                continue
            for asset in pcm.assets:
                heartbeat.check()
                output_ids.append(
                    self.delivery.publish_file(
                        job_id=job_id,
                        role=asset.role,
                        path=asset.path,
                        filename=asset.path.name,
                        content_type="audio/wav",
                        expected_sha256=asset.sha256,
                        published_at_utc=published_at,
                        expires_at_utc=expires_at,
                        media_properties={
                            "audio_format": asset.audio_format,
                            "sample_rate_hz": asset.sample_rate_hz,
                            "channels": asset.channels,
                            "sample_count": asset.sample_count,
                            "segment_index": asset.segment_index,
                            "start_sample": asset.start_sample,
                            "end_sample": asset.end_sample,
                        },
                    )
                )

        if conversions.listening is not None:
            asset = conversions.listening.asset
            heartbeat.check()
            output_ids.append(
                self.delivery.publish_file(
                    job_id=job_id,
                    role="listen",
                    path=asset.path,
                    filename=asset.path.name,
                    content_type="audio/mpeg",
                    expected_sha256=asset.sha256,
                    published_at_utc=published_at,
                    expires_at_utc=expires_at,
                    media_properties={
                        "audio_format": "mp3",
                        "sample_rate_hz": asset.sample_rate_hz,
                        "channels": asset.channels,
                        "bitrate_bps": asset.bitrate_bps,
                        "title": asset.title,
                        "artist": asset.artist,
                    },
                )
            )

        if package_path is not None:
            heartbeat.check()
            output_ids.append(
                self.delivery.publish_file(
                    job_id=job_id,
                    role="package",
                    path=package_path,
                    filename=package_path.name,
                    content_type="application/zip",
                    expected_sha256=sha256_file(package_path),
                    published_at_utc=published_at,
                    expires_at_utc=expires_at,
                    media_properties={"archive_format": "zip", "compression": "stored"},
                )
            )

        self.delivery.repository.mark_available(
            job_id=job_id,
            published_at_utc=published_at,
            expires_at_utc=expires_at,
        )
        return output_ids

    def _current_stage(self, job_id: int) -> str:
        try:
            return str(self.database.get_job(job_id)["processing_state"])
        except KeyError:
            return "unknown"

    def _rollback_unpublished(self, job_id: int) -> None:
        """Best-effort removal of objects uploaded before publication completed."""

        try:
            job = self.database.get_job(job_id)
        except KeyError:
            return
        if str(job.get("delivery_state")) != "not_published":
            return
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, object_key
                FROM outputs
                WHERE job_id = %s AND deleted_at_utc IS NULL
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        for row in rows:
            try:
                self.delivery.storage.delete_object(str(row["object_key"]))
                self.delivery.repository.mark_output_deleted(int(row["id"]))
            except Exception:
                # The output is still inaccessible because delivery was never made available.
                # R2 lifecycle deletion remains the final safety net for an orphaned object.
                continue


def _set_cloud_manifest_profile(
    item_directory: Path,
    cloud_profile: str,
    requested: frozenset[CloudOutput],
) -> None:
    """Record what the cloud job actually asked for, in the item's durable provenance.

    The local services write their own profile alias during acquisition. The manifest
    keeps the Cloud v0.1 preset name and, because a preset no longer describes the
    choice on its own, the exact set of requested outputs beside it.
    """

    manifest_path = item_directory / "metadata" / "archive.json"
    checksum_paths = listed_checksum_paths(item_directory)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Archive manifest is invalid JSON after acquisition") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Archive manifest must be a JSON object")
    request = manifest.get("request")
    if not isinstance(request, dict):
        raise ValueError("Archive manifest has no request metadata")
    outputs = sorted(output.value for output in requested)
    if request.get("profile") == cloud_profile and request.get("cloud_outputs") == outputs:
        return
    request["profile"] = cloud_profile
    request["cloud_outputs"] = outputs
    write_manifest_atomic(manifest_path, manifest)
    write_sha256sums(item_directory, checksum_paths)
    integrity = verify_sha256sums(item_directory)
    if not integrity.valid:
        raise ValueError(
            "Cloud profile provenance update failed integrity verification: "
            + "; ".join(integrity.errors)
        )


def _source_publication_files(acquisition: AcquisitionResult) -> tuple[tuple[Path, str], ...]:
    item = acquisition.item_directory
    candidates = [
        (acquisition.master_path, f"{acquisition.video_id}{acquisition.master_path.suffix.lower()}"),
        (item / "metadata" / "source.info.json", "source.info.json"),
        (item / "metadata" / "archive.json", "archive.json"),
        (item / "checksums" / "SHA256SUMS", "SHA256SUMS"),
        (item / "logs" / "ingest.log", "ingest.log"),
    ]
    artwork = sorted((item / "artwork").glob("source-thumbnail.*"))
    candidates.extend((path, path.name) for path in artwork if path.is_file())
    missing = [str(path) for path, _ in candidates if not path.is_file()]
    if missing:
        raise ValueError("Verified source publication is missing sidecars: " + ", ".join(missing))
    seen: set[Path] = set()
    unique: list[tuple[Path, str]] = []
    for path, filename in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append((path, filename))
    return tuple(unique)


def _create_package(item_directory: Path, output_directory: Path, video_id: str) -> Path:
    integrity = verify_sha256sums(item_directory)
    if not integrity.valid:
        raise ValueError("Cannot package an archive item that failed integrity verification")
    output_directory.mkdir(parents=True, exist_ok=True)
    package_path = output_directory / f"{video_id}.audio-archive.zip"
    if package_path.exists():
        package_path.unlink()
    with ZipFile(package_path, "w", compression=ZIP_STORED, allowZip64=True) as archive:
        files = sorted(path for path in item_directory.rglob("*") if path.is_file())
        for path in files:
            if path.is_symlink():
                raise ValueError("Archive package may not contain symbolic links")
            relative = path.relative_to(item_directory)
            archive.write(path, arcname=relative.as_posix())
    with ZipFile(package_path, "r", allowZip64=True) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise ValueError(f"Archive package verification failed at {corrupt}")
    return package_path


def _content_type(path: Path) -> str:
    overrides = {
        ".webm": "audio/webm",
        ".m4a": "audio/mp4",
        ".mka": "audio/x-matroska",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
        ".json": "application/json",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }
    return overrides.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
