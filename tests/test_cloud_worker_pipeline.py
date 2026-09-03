from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from audio_archive.ableton import AbletonAsset, AbletonResult
from audio_archive.acquisition import AcquisitionRequest, AcquisitionResult
from audio_archive.cloud.config import CloudSettings
from audio_archive.cloud.db import CloudDatabase
from audio_archive.cloud.delivery import DeliveryRepository, TemporaryDeliveryService
from audio_archive.cloud.execution import CloudExecutionRepository
from audio_archive.cloud.models import (
    AccessRetryPolicy,
    CloudJobRequest,
    CloudProfile,
    ProcessingState,
    WorkerNetworkClass,
)
from audio_archive.cloud.pipeline import CloudJobProcessor
from audio_archive.cloud.storage import PublishedObject
from audio_archive.cloud.worker import ClaimHeartbeat, CloudSequentialWorker
from audio_archive.config import AppConfig
from audio_archive.integrity import listed_checksum_paths, write_sha256sums
from audio_archive.tooling import CommandResult, ToolExecutionError
from audio_archive.manifest import write_manifest_atomic
from audio_archive.verify import AudioStream, MediaProbe, sha256_file


VIDEO_ID = "dQw4w9WgXcQ"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


class NeverRunner:
    def run(self, *args, **kwargs):
        raise AssertionError("Fake cloud media services should not execute external tools")


class FakeAcquisitionService:
    def __init__(self, config: AppConfig, runner: object):
        self.config = config

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        item = self.config.archive_root / "items" / "youtube" / request.video_id
        master = item / "master" / f"{request.video_id}.webm"
        source_info = item / "metadata" / "source.info.json"
        manifest_path = item / "metadata" / "archive.json"
        artwork = item / "artwork" / "source-thumbnail.webp"
        ingest_log = item / "logs" / "ingest.log"
        for parent in {master.parent, source_info.parent, artwork.parent, ingest_log.parent}:
            parent.mkdir(parents=True, exist_ok=True)
        master.write_bytes(b"verified-native-source")
        source_info.write_text('{"id":"dQw4w9WgXcQ"}\n', encoding="utf-8")
        artwork.write_bytes(b"thumbnail")
        ingest_log.write_text("{}\n", encoding="utf-8")
        digest = sha256_file(master)
        manifest = {
            "schema_version": "1.2",
            "archive_id": f"youtube:{request.video_id}",
            "content_type": "song",
            "request": {"profile": request.profile},
            "resolution": {
                "method": request.resolution_method,
                "selected_video_id": request.video_id,
                "reviewed_by_user": request.reviewed_by_user,
            },
            "source": {
                "platform": "youtube",
                "id": request.video_id,
                "url": request.url,
                "title": "Fixture source",
                "creator": "Fixture channel",
            },
            "acquisition": {
                "quality_status": "verified_best_available",
                "quality_warnings": [],
            },
            "source_master": {
                "path": f"master/{request.video_id}.webm",
                "sha256": digest,
                "audio_codec": "opus",
                "sample_rate_hz": 48000,
                "channels": 2,
            },
            "intermediates": [],
            "derivatives": [],
        }
        write_manifest_atomic(manifest_path, manifest)
        write_sha256sums(
            item,
            [
                Path(f"master/{request.video_id}.webm"),
                Path("metadata/source.info.json"),
                Path("metadata/archive.json"),
                Path("artwork/source-thumbnail.webp"),
                Path("logs/ingest.log"),
            ],
        )
        probe = MediaProbe(
            format_name="matroska,webm",
            duration_seconds=1.0,
            audio=AudioStream(
                codec="opus",
                sample_rate_hz=48000,
                channels=2,
                bitrate_bps=128000,
            ),
            video_stream_count=0,
        )
        return AcquisitionResult(
            archive_id=f"youtube:{request.video_id}",
            video_id=request.video_id,
            item_directory=item,
            manifest_path=manifest_path,
            master_path=master,
            master_relative_path=f"master/{request.video_id}.webm",
            master_sha256=digest,
            quality_status="verified_best_available",
            warnings=(),
            source_title="Fixture source",
            source_creator="Fixture channel",
            probe=probe,
            reused_existing=False,
        )


class FakeAbletonService:
    def __init__(self, config: AppConfig, runner: object):
        self.config = config

    def create(self, item_directory: Path, *, job_id: int) -> AbletonResult:
        archive_id = json.loads(
            (item_directory / "metadata" / "archive.json").read_text(encoding="utf-8")
        )["archive_id"]
        video_id = str(archive_id).partition(":")[2]
        output = item_directory / "intermediates" / "ableton" / f"{video_id}.wav"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fixture-float-wav")
        digest = sha256_file(output)
        manifest_path = item_directory / "metadata" / "archive.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["intermediates"] = [
            {
                "role": "ableton",
                "path": f"intermediates/ableton/{video_id}.wav",
                "audio_format": "pcm_f32le",
                "sample_rate_hz": 48000,
                "channels": 2,
                "source_sha256": manifest["source_master"]["sha256"],
                "sha256": digest,
                "segment_index": None,
                "start_sample": 0,
                "end_sample": 48000,
            }
        ]
        write_manifest_atomic(manifest_path, manifest)
        checksum_paths = listed_checksum_paths(item_directory)
        write_sha256sums(
            item_directory,
            list(
                dict.fromkeys(
                    checksum_paths
                    + [
                        Path(f"intermediates/ableton/{video_id}.wav"),
                        Path("metadata/archive.json"),
                    ]
                )
            ),
        )
        asset = AbletonAsset(
            relative_path=f"intermediates/ableton/{video_id}.wav",
            path=output,
            sha256=digest,
            sample_rate_hz=48000,
            channels=2,
            sample_count=48000,
            start_sample=0,
            end_sample=48000,
            segment_index=None,
        )
        return AbletonResult(
            archive_id=str(archive_id),
            item_directory=item_directory,
            assets=(asset,),
            segmented=False,
            reused_existing=False,
        )


class FakeDeliveryStorage:
    def __init__(self, *, fail_after: int | None = None):
        self.fail_after = fail_after
        self.publish_calls = 0
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def publish_file(
        self,
        *,
        job_id: int,
        role: str,
        path: Path,
        filename: str,
        content_type: str,
        expected_sha256: str,
    ) -> PublishedObject:
        self.publish_calls += 1
        if self.fail_after is not None and self.publish_calls > self.fail_after:
            raise RuntimeError("simulated R2 publication failure")
        digest = sha256_file(path)
        assert digest == expected_sha256
        suffix = path.suffix.lower()
        key = f"delivery/{job_id}/{role}/{digest}{suffix}"
        self.objects[key] = path.read_bytes()
        return PublishedObject(
            object_key=key,
            filename=filename,
            content_type=content_type,
            size_bytes=path.stat().st_size,
            sha256=digest,
        )

    def create_download_url(self, *, object_key: str, filename: str) -> str:
        return f"https://signed.invalid/{object_key}?filename={filename}"

    def object_exists(self, object_key: str) -> bool:
        return object_key in self.objects

    def delete_object(self, object_key: str) -> None:
        self.objects.pop(object_key, None)
        self.deleted.append(object_key)


class NoopHeartbeat:
    def check(self) -> None:
        return None


@pytest.fixture
def cloud_db() -> CloudDatabase:
    dsn = os.getenv("CLOUD_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("CLOUD_TEST_DATABASE_URL is not configured")
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    database = CloudDatabase(dsn)
    root = Path(__file__).resolve().parents[1]
    assert database.apply_migrations(root / "migrations") == [1, 2]
    return database


@pytest.fixture
def base_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        project_root=tmp_path,
        archive_root=tmp_path / "local-archive-unused",
        temp_directory=tmp_path / "local-temp-unused",
        database_path=tmp_path / "unused.db",
        host="127.0.0.1",
        port=8765,
        open_browser=False,
        poll_interval_seconds=1.0,
        safe_wav_size_gib=1.8,
        segment_minutes=60,
        tools_directory=tmp_path / "tools",
        yt_dlp="yt-dlp",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        deno="deno",
        candidate_limit=5,
        auto_select_min_score=90,
        auto_select_min_margin=15,
        max_csv_bytes=1_000_000,
    )


@pytest.fixture
def cloud_settings(tmp_path: Path, cloud_db: CloudDatabase) -> CloudSettings:
    return CloudSettings(
        database_url=cloud_db.dsn,
        r2_endpoint_url="https://fixture.r2.invalid",
        r2_bucket="fixture",
        r2_access_key_id="fixture-key",
        r2_secret_access_key="fixture-secret",
        scratch_root=tmp_path / "scratch",
        worker_id="fixture-worker",
        worker_network_class=WorkerNetworkClass.CLOUD_DATACENTER,
        retention_hours=24,
        signed_url_ttl_seconds=900,
    )


def _processor(
    cloud_db: CloudDatabase,
    cloud_settings: CloudSettings,
    base_config: AppConfig,
    storage: FakeDeliveryStorage,
) -> CloudJobProcessor:
    delivery = TemporaryDeliveryService(
        repository=DeliveryRepository(cloud_db),
        storage=storage,  # type: ignore[arg-type]
        retention_hours=cloud_settings.retention_hours,
    )
    return CloudJobProcessor(
        database=cloud_db,
        settings=cloud_settings,
        base_config=base_config,
        runner=NeverRunner(),  # type: ignore[arg-type]
        delivery=delivery,
        acquisition_factory=FakeAcquisitionService,  # type: ignore[arg-type]
        ableton_factory=FakeAbletonService,  # type: ignore[arg-type]
    )


def _process_exact_url(
    cloud_db: CloudDatabase,
    cloud_settings: CloudSettings,
    processor: CloudJobProcessor,
    profile: CloudProfile,
):
    job_id = cloud_db.create_job(
        CloudJobRequest(url=VIDEO_URL, profile=profile, origin="url")
    )
    claim = cloud_db.claim_next_job(worker_id=cloud_settings.worker_id)
    assert claim is not None and claim.job_id == job_id
    try:
        result = processor.process_claim(claim, heartbeat=NoopHeartbeat())
    finally:
        cloud_db.release_claim(claim)
    return job_id, result


def test_source_profile_uses_ephemeral_workspace_and_publishes_verified_source_sidecars(
    cloud_db: CloudDatabase,
    cloud_settings: CloudSettings,
    base_config: AppConfig,
) -> None:
    storage = FakeDeliveryStorage()
    processor = _processor(cloud_db, cloud_settings, base_config, storage)

    job_id, result = _process_exact_url(
        cloud_db, cloud_settings, processor, CloudProfile.SOURCE
    )

    assert result.state is ProcessingState.COMPLETED
    job = cloud_db.get_job(job_id)
    assert job["processing_state"] == "completed"
    assert job["delivery_state"] == "available"
    assert job["quality_status"] == "verified_best_available"
    with cloud_db.connect() as connection:
        outputs = connection.execute(
            """
            SELECT role, filename, object_key, deleted_at_utc
            FROM outputs
            WHERE job_id = %s
            ORDER BY id
            """,
            (job_id,),
        ).fetchall()
        attempt = connection.execute(
            "SELECT * FROM processing_attempts WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    assert {row["role"] for row in outputs} == {"source"}
    assert {row["filename"] for row in outputs} >= {
        f"{VIDEO_ID}.webm",
        "source.info.json",
        "archive.json",
        "SHA256SUMS",
        "ingest.log",
        "source-thumbnail.webp",
    }
    archive_output = next(row for row in outputs if row["filename"] == "archive.json")
    published_manifest = json.loads(storage.objects[str(archive_output["object_key"])])
    assert published_manifest["request"]["profile"] == "source"
    assert all(row["deleted_at_utc"] is None for row in outputs)
    assert attempt["result"] == "completed"
    assert not (cloud_settings.scratch_root / f"job-{job_id}").exists()


def test_ableton_profile_publishes_source_and_verified_wav(
    cloud_db: CloudDatabase,
    cloud_settings: CloudSettings,
    base_config: AppConfig,
) -> None:
    storage = FakeDeliveryStorage()
    processor = _processor(cloud_db, cloud_settings, base_config, storage)

    job_id, result = _process_exact_url(
        cloud_db, cloud_settings, processor, CloudProfile.ABLETON
    )

    assert result.state is ProcessingState.COMPLETED
    with cloud_db.connect() as connection:
        outputs = connection.execute(
            "SELECT role, filename, media_properties_json FROM outputs WHERE job_id = %s ORDER BY id",
            (job_id,),
        ).fetchall()
    assert {row["role"] for row in outputs} == {"source", "ableton"}
    ableton = next(row for row in outputs if row["role"] == "ableton")
    assert ableton["filename"] == f"{VIDEO_ID}.wav"
    assert ableton["media_properties_json"]["audio_format"] == "pcm_f32le"
    assert ableton["media_properties_json"]["sample_rate_hz"] == 48000


def test_package_profile_adds_verified_zip_without_changing_audio_pipeline(
    cloud_db: CloudDatabase,
    cloud_settings: CloudSettings,
    base_config: AppConfig,
) -> None:
    storage = FakeDeliveryStorage()
    processor = _processor(cloud_db, cloud_settings, base_config, storage)

    job_id, result = _process_exact_url(
        cloud_db, cloud_settings, processor, CloudProfile.PACKAGE
    )

    assert result.state is ProcessingState.COMPLETED
    with cloud_db.connect() as connection:
        outputs = connection.execute(
            "SELECT role, filename FROM outputs WHERE job_id = %s ORDER BY id",
            (job_id,),
        ).fetchall()
    roles = {row["role"] for row in outputs}
    assert roles == {"source", "ableton", "package"}
    package = next(row for row in outputs if row["role"] == "package")
    assert package["filename"] == f"{VIDEO_ID}.audio-archive.zip"


def test_publication_failure_never_makes_partial_outputs_downloadable(
    cloud_db: CloudDatabase,
    cloud_settings: CloudSettings,
    base_config: AppConfig,
) -> None:
    storage = FakeDeliveryStorage(fail_after=1)
    processor = _processor(cloud_db, cloud_settings, base_config, storage)
    job_id = cloud_db.create_job(
        CloudJobRequest(url=VIDEO_URL, profile=CloudProfile.SOURCE, origin="url")
    )
    claim = cloud_db.claim_next_job(worker_id=cloud_settings.worker_id)
    assert claim is not None

    with pytest.raises(RuntimeError, match="simulated R2 publication failure"):
        processor.process_claim(claim, heartbeat=NoopHeartbeat())
    cloud_db.release_claim(claim)

    job = cloud_db.get_job(job_id)
    assert job["processing_state"] == "failed"
    assert job["delivery_state"] == "not_published"
    assert job["error_stage"] == "publishing"
    assert storage.objects == {}
    with cloud_db.connect() as connection:
        rows = connection.execute(
            "SELECT deleted_at_utc FROM outputs WHERE job_id = %s",
            (job_id,),
        ).fetchall()
        attempt = connection.execute(
            "SELECT result, error_class FROM processing_attempts WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    assert rows and all(row["deleted_at_utc"] is not None for row in rows)
    assert attempt["result"] == "failed"
    assert attempt["error_class"] == "RuntimeError"
    assert not (cloud_settings.scratch_root / f"job-{job_id}").exists()


def test_abandoned_active_job_is_requeued_from_pinned_source(cloud_db: CloudDatabase) -> None:
    job_id = cloud_db.create_job(
        CloudJobRequest(url=VIDEO_URL, profile=CloudProfile.SOURCE, origin="url")
    )
    cloud_db.transition_processing(job_id, ProcessingState.DOWNLOADING)
    with cloud_db.connect() as connection:
        connection.execute(
            """
            INSERT INTO worker_claims (
                job_id, worker_id, claim_token, claimed_at_utc,
                heartbeat_at_utc, lease_expires_at_utc
            ) VALUES (
                %s, 'dead-worker', %s,
                NOW() - INTERVAL '10 minutes',
                NOW() - INTERVAL '10 minutes',
                NOW() - INTERVAL '5 minutes'
            )
            """,
            (job_id, uuid4()),
        )
        connection.execute(
            """
            INSERT INTO processing_attempts (job_id, worker_id, worker_network_class)
            VALUES (%s, 'dead-worker', 'cloud_datacenter')
            """,
            (job_id,),
        )

    recovered = CloudExecutionRepository(cloud_db).recover_abandoned_jobs()

    assert recovered == (job_id,)
    assert cloud_db.get_job(job_id)["processing_state"] == "ready"
    with cloud_db.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM worker_claims WHERE job_id = %s", (job_id,)
        ).fetchone() is None
        attempt = connection.execute(
            "SELECT result, error_class FROM processing_attempts WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    assert attempt["result"] == "interrupted"
    assert attempt["error_class"] == "WorkerLeaseExpired"


def test_worker_claim_heartbeat_renews_lease(cloud_db: CloudDatabase) -> None:
    job_id = cloud_db.create_job(
        CloudJobRequest(url=VIDEO_URL, profile=CloudProfile.SOURCE, origin="url")
    )
    claim = cloud_db.claim_next_job(worker_id="heartbeat-worker", lease_seconds=2)
    assert claim is not None and claim.job_id == job_id
    with cloud_db.connect() as connection:
        before = connection.execute(
            "SELECT heartbeat_at_utc FROM worker_claims WHERE job_id = %s", (job_id,)
        ).fetchone()["heartbeat_at_utc"]

    with ClaimHeartbeat(
        cloud_db,
        claim,
        lease_seconds=2,
        interval_seconds=0.05,
    ):
        time.sleep(0.15)

    with cloud_db.connect() as connection:
        after = connection.execute(
            "SELECT heartbeat_at_utc FROM worker_claims WHERE job_id = %s", (job_id,)
        ).fetchone()["heartbeat_at_utc"]
    assert after > before
    cloud_db.release_claim(claim)


def test_sequential_worker_claims_runs_and_releases_job(
    cloud_db: CloudDatabase,
    cloud_settings: CloudSettings,
    base_config: AppConfig,
) -> None:
    storage = FakeDeliveryStorage()
    processor = _processor(cloud_db, cloud_settings, base_config, storage)
    job_id = cloud_db.create_job(
        CloudJobRequest(url=VIDEO_URL, profile=CloudProfile.SOURCE, origin="url")
    )
    worker = CloudSequentialWorker(
        database=cloud_db,
        settings=cloud_settings,
        processor=processor,
        lease_seconds=2,
        heartbeat_interval_seconds=0.1,
    )

    result = worker.run_next()

    assert result is not None
    assert result.job_id == job_id
    assert result.state is ProcessingState.COMPLETED
    assert result.error is None
    with cloud_db.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM worker_claims WHERE job_id = %s", (job_id,)
        ).fetchone() is None


RATE_LIMITED_STDERR = (
    "WARNING: [youtube] dQw4w9WgXcQ: Unable to download webpage: "
    "HTTP Error 429: Too Many Requests"
)
POLICY = AccessRetryPolicy(limit=2, base_seconds=300)


def _tool_failure(stderr: str) -> ToolExecutionError:
    return ToolExecutionError(
        CommandResult(
            argv=("yt-dlp", VIDEO_URL),
            returncode=1,
            stdout="",
            stderr=stderr,
            started_at_utc="2026-09-03T00:00:00+00:00",
            finished_at_utc="2026-09-03T00:00:13+00:00",
        )
    )


def _pinned_job(cloud_db: CloudDatabase, state: ProcessingState) -> int:
    job_id = cloud_db.create_job(
        CloudJobRequest(url=VIDEO_URL, profile=CloudProfile.SOURCE, origin="url")
    )
    cloud_db.transition_processing(job_id, state)
    return job_id


def test_rate_limited_download_is_requeued_instead_of_left_failed(
    cloud_db: CloudDatabase,
) -> None:
    job_id = _pinned_job(cloud_db, ProcessingState.DOWNLOADING)

    outcome = CloudExecutionRepository(cloud_db).fail_job(
        job_id=job_id,
        stage="downloading",
        error=_tool_failure(RATE_LIMITED_STDERR),
        retry_policy=POLICY,
    )

    assert outcome.recorded
    assert outcome.error_class == "SourceAccessRateLimited"
    assert outcome.retry_attempt == 1
    job = cloud_db.get_job(job_id)
    assert job["processing_state"] == ProcessingState.READY.value
    assert job["access_retry_count"] == 1
    assert job["retry_not_before_utc"] is not None
    # The failure detail is cleared from the job because the job is runnable again;
    # the history keeps the record.
    assert job["error_class"] is None
    with cloud_db.connect() as connection:
        events = connection.execute(
            "SELECT event_type FROM job_events WHERE job_id = %s ORDER BY id",
            (job_id,),
        ).fetchall()
    assert [row["event_type"] for row in events][-2:] == ["failed", "access_retry_scheduled"]


def test_a_job_waiting_out_its_backoff_is_not_claimable(cloud_db: CloudDatabase) -> None:
    job_id = _pinned_job(cloud_db, ProcessingState.DOWNLOADING)
    CloudExecutionRepository(cloud_db).fail_job(
        job_id=job_id,
        stage="downloading",
        error=_tool_failure(RATE_LIMITED_STDERR),
        retry_policy=POLICY,
    )

    assert cloud_db.claim_next_job(worker_id="worker-1") is None

    with cloud_db.connect() as connection:
        connection.execute(
            "UPDATE jobs SET retry_not_before_utc = NOW() - INTERVAL '1 second' WHERE id = %s",
            (job_id,),
        )

    claim = cloud_db.claim_next_job(worker_id="worker-1")
    assert claim is not None and claim.job_id == job_id


def test_the_automatic_retry_budget_is_finite(cloud_db: CloudDatabase) -> None:
    job_id = _pinned_job(cloud_db, ProcessingState.DOWNLOADING)
    execution = CloudExecutionRepository(cloud_db)
    with cloud_db.connect() as connection:
        connection.execute(
            "UPDATE jobs SET access_retry_count = %s WHERE id = %s", (POLICY.limit, job_id)
        )

    outcome = execution.fail_job(
        job_id=job_id,
        stage="downloading",
        error=_tool_failure(RATE_LIMITED_STDERR),
        retry_policy=POLICY,
    )

    assert outcome.retry_at_utc is None
    job = cloud_db.get_job(job_id)
    assert job["processing_state"] == ProcessingState.FAILED.value
    assert job["error_class"] == "SourceAccessRateLimited"
    assert job["retry_not_before_utc"] is None


def test_a_conversion_failure_is_never_requeued_automatically(cloud_db: CloudDatabase) -> None:
    job_id = _pinned_job(cloud_db, ProcessingState.DOWNLOADING)
    cloud_db.transition_processing(job_id, ProcessingState.VERIFYING_MASTER)
    cloud_db.transition_processing(job_id, ProcessingState.CONVERTING)

    outcome = CloudExecutionRepository(cloud_db).fail_job(
        job_id=job_id,
        stage="converting",
        error=_tool_failure("Error opening output file: Invalid data found"),
        retry_policy=POLICY,
    )

    assert outcome.error_class == "ToolExecutionError"
    assert outcome.retry_at_utc is None
    assert cloud_db.get_job(job_id)["processing_state"] == ProcessingState.FAILED.value


def test_an_unavailable_source_is_never_requeued_automatically(cloud_db: CloudDatabase) -> None:
    job_id = _pinned_job(cloud_db, ProcessingState.DOWNLOADING)

    outcome = CloudExecutionRepository(cloud_db).fail_job(
        job_id=job_id,
        stage="downloading",
        error=_tool_failure("ERROR: [youtube] dQw4w9WgXcQ: Video unavailable"),
        retry_policy=POLICY,
    )

    assert outcome.error_class == "SourceUnavailable"
    assert outcome.retry_at_utc is None
    assert cloud_db.get_job(job_id)["processing_state"] == ProcessingState.FAILED.value
