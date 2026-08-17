from __future__ import annotations

from pathlib import Path

from .ableton import AbletonResult, AbletonService
from .acquisition import AcquisitionRequest, AcquisitionResult, AcquisitionService
from .config import AppConfig
from .db import ArchiveDatabase
from .models import JobState
from .tooling import CommandRunner


def acquire_ready_job(
    database: ArchiveDatabase,
    config: AppConfig,
    runner: CommandRunner,
    job_id: int,
) -> AcquisitionResult:
    job = database.get_job(job_id)
    if JobState(job["state"]) != JobState.READY:
        raise ValueError(f"Job {job_id} must be ready; current state is {job['state']}")
    if job["source_extractor"] != "youtube" or not job["source_id"] or not job["source_url"]:
        raise ValueError(f"Job {job_id} does not have a pinned YouTube source")

    database.transition_job(job_id, JobState.DOWNLOADING, message="Native acquisition started")
    try:
        result = AcquisitionService(config, runner).acquire(
            AcquisitionRequest(
                job_id=job_id,
                video_id=job["source_id"],
                url=job["source_url"],
                profile=job["profile"],
                artist=job["requested_artist"],
                title=job["requested_title"],
                version=job["requested_version"],
                origin=job["origin"],
                import_filename=job["import_filename"],
                import_file_sha256=job["import_file_sha256"],
                import_row=job["import_row"],
                resolution_method=job["resolution_method"] or "exact_url",
                selected_score=job["selected_score"],
                runner_up_score=job["runner_up_score"],
                reviewed_by_user=(job["resolution_method"] == "manual_selection"),
            )
        )
        database.transition_job(
            job_id,
            JobState.VERIFYING_MASTER,
            message="Acquired media; source-master verification passed",
            detail={"reused_existing": result.reused_existing},
        )
        database.record_acquisition(
            job_id,
            archive_id=result.archive_id,
            source_id=result.video_id,
            source_title=result.source_title,
            source_creator=result.source_creator,
            item_directory=str(result.item_directory),
            manifest_path=str(result.manifest_path),
            quality_status=result.quality_status,
            master_relative_path=result.master_relative_path,
            master_sha256=result.master_sha256,
            media_properties={
                "format_name": result.probe.format_name,
                "duration_seconds": result.probe.duration_seconds,
                "audio_codec": result.probe.audio.codec,
                "sample_rate_hz": result.probe.audio.sample_rate_hz,
                "channels": result.probe.audio.channels,
                "bitrate_bps": result.probe.audio.bitrate_bps,
            },
            warnings=[warning.message for warning in result.warnings],
        )
        if job["profile"] == "archive":
            final_state = (
                JobState.COMPLETED
                if result.quality_status == "verified_best_available"
                else JobState.COMPLETED_WITH_WARNINGS
            )
            database.transition_job(job_id, final_state, message="Archive profile completed")
        else:
            database.transition_job(
                job_id,
                JobState.CONVERTING,
                message="Verified master ready for requested derivative creation",
            )
        return result
    except Exception as exc:
        current = JobState(database.get_job(job_id)["state"])
        if current in {JobState.DOWNLOADING, JobState.VERIFYING_MASTER}:
            database.fail_job(job_id, stage=current.value, summary=str(exc))
        raise


def create_ableton_for_job(
    database: ArchiveDatabase,
    config: AppConfig,
    runner: CommandRunner,
    job_id: int,
) -> AbletonResult:
    job = database.get_job(job_id)
    if JobState(job["state"]) != JobState.CONVERTING:
        raise ValueError(f"Job {job_id} must be converting; current state is {job['state']}")
    if job["profile"] not in {"ableton", "complete"}:
        raise ValueError(f"Job profile {job['profile']} does not request an Ableton output")
    if job["source_extractor"] != "youtube" or not job["source_id"]:
        raise ValueError(f"Job {job_id} does not have a pinned YouTube source")
    archive_item = database.find_archive_item("youtube", job["source_id"])
    if archive_item is None:
        raise ValueError(f"Job {job_id} has no verified archive item")

    try:
        result = AbletonService(config, runner).create(
            Path(archive_item["item_directory"]),
            job_id=job_id,
        )
        database.record_ableton_outputs(
            job_id,
            archive_id=result.archive_id,
            assets=[
                {
                    "relative_path": asset.relative_path,
                    "sha256": asset.sha256,
                    "media_properties": {
                        "audio_format": "pcm_f32le",
                        "sample_rate_hz": asset.sample_rate_hz,
                        "channels": asset.channels,
                        "sample_count": asset.sample_count,
                        "start_sample": asset.start_sample,
                        "end_sample": asset.end_sample,
                        "segment_index": asset.segment_index,
                    },
                }
                for asset in result.assets
            ],
            reused_existing=result.reused_existing,
        )
        if job["profile"] == "complete":
            return result
        database.transition_job(
            job_id,
            JobState.VERIFYING_OUTPUT,
            message="Ableton output probe and integrity verification passed",
            detail={"segmented": result.segmented, "asset_count": len(result.assets)},
        )
        final_state = (
            JobState.COMPLETED
            if job["quality_status"] == "verified_best_available"
            else JobState.COMPLETED_WITH_WARNINGS
        )
        database.transition_job(job_id, final_state, message="Ableton profile completed")
        return result
    except Exception as exc:
        current = JobState(database.get_job(job_id)["state"])
        if current in {JobState.CONVERTING, JobState.VERIFYING_OUTPUT}:
            database.fail_job(job_id, stage=current.value, summary=str(exc))
        raise
