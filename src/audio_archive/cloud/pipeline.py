from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol

from ..ableton import AbletonResult, AbletonService
from ..acquisition import AcquisitionRequest, AcquisitionResult, AcquisitionService
from ..config import AppConfig
from ..integrity import listed_checksum_paths, verify_sha256sums, write_sha256sums
from ..manifest import write_manifest_atomic
from ..resolver import decide_resolution
from ..source_resolution import search_youtube_candidates
from ..tooling import CommandRunner, ToolExecutionError
from .db import CloudDatabase
from .delivery import TemporaryDeliveryService
from .jobs import CloudJobRepository
from .models import CloudProfile, ProcessingState, WorkerClaim
from .package import ArchivePackage, build_archive_package
from .workspace import CloudJobWorkspace


class AcquisitionLike(Protocol):
    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult: ...


class AbletonLike(Protocol):
    def create(self, item_directory: Path, *, job_id: int) -> AbletonResult: ...


@dataclass(frozen=True)
class CloudPipelineResult:
    job_id: int
    state: ProcessingState
    output_ids: tuple[int, ...] = ()
    tool_versions: dict[str, str | None] | None = None


class CloudPipeline:
    def __init__(
        self,
        *,
        database: CloudDatabase,
        base_config: AppConfig,
        runner: CommandRunner,
        delivery: TemporaryDeliveryService,
        scratch_root: Path,
        acquisition_factory: Callable[[AppConfig, CommandRunner], AcquisitionLike] = AcquisitionService,
        ableton_factory: Callable[[AppConfig, CommandRunner], AbletonLike] = AbletonService,
        package_builder: Callable[[Path, Path], ArchivePackage] = build_archive_package,
    ) -> None:
        self.database = database
        self.jobs = CloudJobRepository(database)
        self.base_config = base_config
        self.runner = runner
        self.delivery = delivery
        self.scratch_root = scratch_root
        self.acquisition_factory = acquisition_factory
        self.ableton_factory = ableton_factory
        self.package_builder = package_builder

    def process(self, claim: WorkerClaim) -> CloudPipelineResult:
        job_id = claim.job_id
        workspace = CloudJobWorkspace(self.scratch_root, job_id)
        config = workspace.app_config(self.base_config)
        self._rollback_unpublished_delivery(job_id)
        job = self.database.get_job(job_id)
        state = ProcessingState(str(job["processing_state"]))

        if state is ProcessingState.PENDING:
            state = self._resolve(job_id, config)
            if state in {ProcessingState.NEEDS_REVIEW, ProcessingState.NOT_FOUND}:
                return CloudPipelineResult(job_id, state)
            job = self.database.get_job(job_id)

        if state is not ProcessingState.READY:
            raise ValueError(f"Claimed job {job_id} is not processable from state {state.value}")

        acquisition = self._acquire(job_id, config)
        profile = CloudProfile(str(self.database.get_job(job_id)["profile"]))
        _record_cloud_profile(acquisition.item_directory, profile, _pipeline_profile(profile))

        ableton: AbletonResult | None = None
        package: ArchivePackage | None = None
        if profile in {CloudProfile.ABLETON, CloudProfile.PACKAGE}:
            self.database.transition_processing(
                job_id,
                ProcessingState.CONVERTING,
                message="Verified source master ready for Ableton conversion",
            )
            ableton = self.ableton_factory(config, self.runner).create(
                acquisition.item_directory,
                job_id=job_id,
            )
            self.database.transition_processing(
                job_id,
                ProcessingState.VERIFYING_OUTPUT,
                message="Ableton output passed service verification",
            )

        if profile is CloudProfile.PACKAGE:
            self.database.transition_processing(
                job_id,
                ProcessingState.PACKAGING,
                message="Building complete local handoff package from verified assets",
            )
            package = self.package_builder(
                acquisition.item_directory,
                workspace.root / "package" / f"{acquisition.video_id}-archive.zip",
            )

        current = ProcessingState(str(self.database.get_job(job_id)["processing_state"]))
        if current not in {
            ProcessingState.VERIFYING_MASTER,
            ProcessingState.VERIFYING_OUTPUT,
            ProcessingState.PACKAGING,
        }:
            raise ValueError(f"Job {job_id} cannot publish from state {current.value}")
        self.database.transition_processing(
            job_id,
            ProcessingState.PUBLISHING,
            message="Publishing verified outputs to private temporary delivery storage",
        )

        try:
            output_ids = self._publish(
                job_id=job_id,
                profile=profile,
                acquisition=acquisition,
                ableton=ableton,
                package=package,
            )
        except Exception:
            self._rollback_unpublished_delivery(job_id)
            raise

        final = (
            ProcessingState.COMPLETED
            if acquisition.quality_status == "verified_best_available"
            else ProcessingState.COMPLETED_WITH_WARNINGS
        )
        self.database.transition_processing(
            job_id,
            final,
            message=f"{profile.value} cloud profile completed and published",
        )
        tool_versions = _tool_versions(acquisition.manifest_path)
        workspace.cleanup()
        return CloudPipelineResult(job_id, final, tuple(output_ids), tool_versions)

    def _resolve(self, job_id: int, config: AppConfig) -> ProcessingState:
        job = self.database.get_job(job_id)
        artist = str(job["requested_artist"] or "").strip()
        title = str(job["requested_title"] or "").strip()
        version = str(job["requested_version"] or "").strip() or None
        if not artist or not title:
            raise ValueError("Pending cloud job has no artist/title request")

        self.database.transition_processing(
            job_id,
            ProcessingState.RESOLVING,
            message=f"Searching up to {config.candidate_limit} YouTube candidates",
        )
        candidates = search_youtube_candidates(
            config,
            self.runner,
            artist=artist,
            title=title,
            version=version,
        )
        decision = decide_resolution(
            artist=artist,
            title=title,
            version=version,
            candidates=list(candidates),
            minimum_score=config.auto_select_min_score,
            minimum_margin=config.auto_select_min_margin,
        )
        return self.jobs.record_resolution(job_id, decision)

    def _acquire(self, job_id: int, config: AppConfig) -> AcquisitionResult:
        job = self.database.get_job(job_id)
        if job["source_extractor"] != "youtube" or not job["source_id"] or not job["source_url"]:
            raise ValueError(f"Cloud job {job_id} has no approved YouTube source")
        profile = CloudProfile(str(job["profile"]))
        self.database.transition_processing(
            job_id,
            ProcessingState.DOWNLOADING,
            message="Native source acquisition started in isolated cloud workspace",
        )
        result = self.acquisition_factory(config, self.runner).acquire(
            AcquisitionRequest(
                job_id=job_id,
                video_id=str(job["source_id"]),
                url=str(job["source_url"]),
                profile=_pipeline_profile(profile),
                artist=str(job["requested_artist"]) if job["requested_artist"] else None,
                title=str(job["requested_title"]) if job["requested_title"] else None,
                version=str(job["requested_version"]) if job["requested_version"] else None,
                origin=str(job["origin"]),
                import_row=int(job["import_row"]) if job["import_row"] is not None else None,
                resolution_method=str(job["resolution_method"] or "exact_url"),
                selected_score=int(job["selected_score"]) if job["selected_score"] is not None else None,
                runner_up_score=int(job["runner_up_score"]) if job["runner_up_score"] is not None else None,
                reviewed_by_user=(job["resolution_method"] == "manual_selection"),
            )
        )
        self.database.transition_processing(
            job_id,
            ProcessingState.VERIFYING_MASTER,
            message="Acquired source master passed verification",
        )
        self.jobs.record_acquisition(job_id, result)
        return result

    def _publish(
        self,
        *,
        job_id: int,
        profile: CloudProfile,
        acquisition: AcquisitionResult,
        ableton: AbletonResult | None,
        package: ArchivePackage | None,
    ) -> list[int]:
        published_at = datetime.now(UTC)
        expires_at = self.delivery.default_expiry(published_at)
        job = self.database.get_job(job_id)
        stem = _download_stem(job, acquisition.video_id)
        output_ids: list[int] = []

        output_ids.append(
            self.delivery.publish_file(
                job_id=job_id,
                role="source",
                path=acquisition.master_path,
                filename=f"{stem}{acquisition.master_path.suffix.lower()}",
                content_type=_content_type(acquisition.master_path),
                expected_sha256=acquisition.master_sha256,
                published_at_utc=published_at,
                expires_at_utc=expires_at,
                media_properties={
                    "format_name": acquisition.probe.format_name,
                    "duration_seconds": acquisition.probe.duration_seconds,
                    "audio_codec": acquisition.probe.audio.codec,
                    "sample_rate_hz": acquisition.probe.audio.sample_rate_hz,
                    "channels": acquisition.probe.audio.channels,
                    "quality_status": acquisition.quality_status,
                },
            )
        )

        if profile in {CloudProfile.ABLETON, CloudProfile.PACKAGE}:
            if ableton is None:
                raise ValueError("Ableton profile reached publication without verified Ableton output")
            for asset in ableton.assets:
                segment = f".part-{asset.segment_index:03d}" if asset.segment_index is not None else ""
                output_ids.append(
                    self.delivery.publish_file(
                        job_id=job_id,
                        role="ableton",
                        path=asset.path,
                        filename=f"{stem}{segment}.wav",
                        content_type="audio/wav",
                        expected_sha256=asset.sha256,
                        published_at_utc=published_at,
                        expires_at_utc=expires_at,
                        media_properties={
                            "audio_format": "pcm_f32le",
                            "sample_rate_hz": asset.sample_rate_hz,
                            "channels": asset.channels,
                            "sample_count": asset.sample_count,
                            "segment_index": asset.segment_index,
                            "start_sample": asset.start_sample,
                            "end_sample": asset.end_sample,
                        },
                    )
                )

        if profile is CloudProfile.PACKAGE:
            if package is None:
                raise ValueError("Package profile reached publication without a verified ZIP")
            output_ids.append(
                self.delivery.publish_file(
                    job_id=job_id,
                    role="package",
                    path=package.path,
                    filename=f"{stem} - archive.zip",
                    content_type="application/zip",
                    expected_sha256=package.sha256,
                    published_at_utc=published_at,
                    expires_at_utc=expires_at,
                    media_properties={"zip64": True, "compression": "stored"},
                )
            )

        self.delivery.repository.mark_available(
            job_id=job_id,
            published_at_utc=published_at,
            expires_at_utc=expires_at,
        )
        return output_ids

    def _rollback_unpublished_delivery(self, job_id: int) -> None:
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT delivery_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            if job["delivery_state"] != "not_published":
                return
            rows = connection.execute(
                "SELECT id, object_key FROM outputs WHERE job_id = %s AND deleted_at_utc IS NULL",
                (job_id,),
            ).fetchall()

        removed: list[int] = []
        for row in rows:
            object_key = str(row["object_key"])
            if self.delivery.storage.object_exists(object_key):
                self.delivery.storage.delete_object(object_key)
            removed.append(int(row["id"]))
        if removed:
            with self.database.connect() as connection:
                connection.execute(
                    "DELETE FROM outputs WHERE job_id = %s AND id = ANY(%s)",
                    (job_id, removed),
                )


def classify_pipeline_failure(stage: ProcessingState, exc: Exception) -> str:
    text = str(exc).casefold()
    if isinstance(exc, ToolExecutionError):
        detail = f"{exc.result.stdout}\n{exc.result.stderr}".casefold()
        if "403" in detail or "forbidden" in detail:
            return "youtube_access_403"
        if "po token" in detail or "pot" in detail and "token" in detail:
            return "youtube_po_token"
    if stage in {ProcessingState.RESOLVING, ProcessingState.DOWNLOADING}:
        return "source_access"
    if stage in {ProcessingState.CONVERTING, ProcessingState.VERIFYING_OUTPUT}:
        return "output_processing"
    if stage is ProcessingState.PUBLISHING:
        return "delivery_publication"
    if "checksum" in text or "sha-256" in text or "verification" in text:
        return "verification"
    return "processing"


def _pipeline_profile(profile: CloudProfile) -> str:
    if profile is CloudProfile.SOURCE:
        return "archive"
    return "ableton"


def _record_cloud_profile(item_directory: Path, profile: CloudProfile, pipeline_profile: str) -> None:
    manifest_path = item_directory / "metadata" / "archive.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    request = manifest.get("request")
    if not isinstance(request, dict):
        raise ValueError("Archive manifest request field is invalid")
    request["profile"] = profile.value
    request["pipeline_profile"] = pipeline_profile
    manifest["request"] = request
    checksum_paths = listed_checksum_paths(item_directory)
    write_manifest_atomic(manifest_path, manifest)
    write_sha256sums(item_directory, checksum_paths)
    integrity = verify_sha256sums(item_directory)
    if not integrity.valid:
        raise ValueError("Cloud profile metadata update failed archive integrity verification")


def _tool_versions(manifest_path: Path) -> dict[str, str | None]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    acquisition = manifest.get("acquisition")
    if not isinstance(acquisition, dict):
        return {}
    return {
        "yt_dlp": _optional_text(acquisition.get("yt_dlp_version")),
        "ffmpeg": _optional_text(acquisition.get("ffmpeg_version")),
        "ffprobe": _optional_text(acquisition.get("ffprobe_version")),
        "deno": _optional_text(acquisition.get("deno_version")),
    }


def _optional_text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _download_stem(job: dict[str, object], fallback: str) -> str:
    artist = str(job.get("requested_artist") or "").strip()
    title = str(job.get("requested_title") or "").strip()
    candidate = " - ".join(part for part in (artist, title) if part) or fallback
    candidate = candidate.replace("/", "-").replace("\\", "-")
    return "".join(character for character in candidate if ord(character) >= 32).strip() or fallback


def _content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"
