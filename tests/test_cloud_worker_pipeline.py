from __future__ import annotations

import os
import time
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import psycopg
import pytest

from audio_archive.ableton import AbletonAsset, AbletonResult
from audio_archive.acquisition import AcquisitionRequest, AcquisitionResult
from audio_archive.cloud.db import CloudDatabase, LostWorkerClaim
from audio_archive.cloud.delivery import DeliveryRepository, TemporaryDeliveryService
from audio_archive.cloud.jobs import CloudJobRepository
from audio_archive.cloud.models import (
    CloudJobRequest,
    CloudProfile,
    ProcessingState,
    WorkerNetworkClass,
)
from audio_archive.cloud.package import build_archive_package
from audio_archive.cloud.pipeline import CloudPipeline
from audio_archive.cloud.storage import PublishedObject, object_key_for
from audio_archive.cloud.worker import CloudSequentialWorker, LeaseKeeper
from audio_archive.config import AppConfig
from audio_archive.integrity import write_sha256sums
from audio_archive.manifest import write_manifest_atomic
from audio_archive.verify import AudioStream, MediaProbe, sha256_file


VIDEO_ID = "dQw4w9WgXcQ"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


class NoopRunner:
    def run(self, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        raise AssertionError(f"Unexpected tool invocation in orchestration test: {argv}")


class FakeAcquisitionService:
    def __init__(self, config: AppConfig, runner: object):
        self.config = config

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        item = self.config.archive_root / "items" / "youtube" / request.video_id
        master_relative = Path("master") / f"{request.video_id}.webm"
        master = item / master_relative
        master.parent.mkdir(parents=True, exist_ok=True)
        master.write_bytes(b"verified native opus fixture")
        digest = sha256_file(master)
        probe = MediaProbe(
            format_name="matroska,webm",
            duration_seconds=180.0,
            audio=AudioStream("opus", 48000, 2, 128000),
            video_stream_count=0,
        )
        manifest = {
            "schema_version": 1,
            "archive_id": f"youtube:{request.video_id}",
            "request": {"profile": request.profile},
            "source": {
                "platform": "youtube",
                "id": request.video_id,
                "url": request.url,
                "title": "Fixture Song",
                "creator": "Fixture Artist",
            },
            "acquisition": {
                "quality_status": "verified_best_available",
                "yt_dlp_version": "2026.7.4",
                "ffmpeg_version": "8.0",
                "ffprobe_version": "8.0",
                "deno_version": "2.3.7",
                "quality_warnings": [],
            },
            "source_master": {
                "path": master_relative.as_posix(),
                "sha256": digest,
                "audio_codec": "opus",
                "sample_rate_hz": 48000,
                "channels": 2,
            },
            "intermediates": [],
            "derivatives": [],
        }
        manifest_path = item / "metadata" / "archive.json"
        write_manifest_atomic(manifest_path, manifest)
        write_sha256sums(item, [master_relative, Path("metadata/archive.json")])
        return AcquisitionResult(
            archive_id=f"youtube:{request.video_id}",
            video_id=request.video_id,
            item_directory=item,
            manifest_path=manifest_path,
            master_path=master,
            master_relative_path=master_relative.as_posix(),
            master_sha256=digest,
            quality_status="verified_best_available",
            warnings=(),
            source_title="Fixture Song",
            source_creator="Fixture Artist",
            probe=probe,
            reused_existing=False,
        )


class FakeAbletonService:
    def __init__(self, config: AppConfig, runner: object):
        self.config = config

    def create(self, item_directory: Path, *, job_id: int) -> AbletonResult:
        path = item_directory / "intermediates" / "ableton" / f"{VIDEO_ID}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"verified 32-bit float WAV fixture")
        asset = AbletonAsset(
            relative_path=f"intermediates/ableton/{VIDEO_ID}.wav",
            path=path,
            sha256=sha256_file(path),
            sample_rate_hz=48000,
            channels=2,
            sample_count=8_640_000,
            start_sample=0,
            end_sample=8_640_000,
            segment_index=None,
        )
        return AbletonResult(
            archive_id=f"youtube:{VIDEO_ID}",
            item_directory=item_directory,
            assets=(asset,),
            segmented=False,
            reused_existing=False,
        )


class FakeStorage:
    def __init__(self, *, fail_role: str | None = None):
        self.fail_role = fail_role
        self.objects: dict[str, PublishedObject] = {}
        self.deleted: list[str] = []

    def publish_file(
        self,
        *,
        job_id: int,
        role: str,
        path: Path,
        filename: str,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> PublishedObject:
        if role == self.fail_role:
            raise RuntimeError(f"simulated {role} publication failure")
        digest = sha256_file(path)
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("fixture digest mismatch")
        key = object_key_for(job_id=job_id, role=role, sha256=digest, suffix=path.suffix)
        published = PublishedObject(
            object_key=key,
            filename=filename,
            content_type=content_type,
            size_bytes=path.stat().st_size,
            sha256=digest,
        )
        self.objects[key] = published
        return published

    def object_exists(self, object_key: str) -> bool:
        return object_key in self.objects

    def delete_object(self, object_key: str) -> None:
        self.objects.pop(object_key, None)
        self.deleted.append(object_key)

    def create_download_url(self, *, object_key: str, filename: str) -> str:
        return f"https://signed.example/{object_key}"


@pytest.fixture
def cloud_db() -> CloudDatabase:
    dsn = os.getenv("CLOUD_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("CLOUD_TEST_DATABASE_URL is not configured")
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    database = CloudDatabase(dsn)
    root = Path(__file__).resolve().parents[1]
    assert database.apply_migrations(root / "migrations") == [1]
    return database


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        project_root=tmp_path,
        archive_root=tmp_path / "local-archive-not-used",
        temp_directory=tmp_path / "local-temp-not-used",
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
        max_csv_bytes=5_242_880,
    )


def _pipeline(
    database: CloudDatabase,
    tmp_path: Path,
    storage: FakeStorage,
) -> CloudPipeline:
    delivery = TemporaryDeliveryService(
        repository=DeliveryRepository(database),
        storage=storage,  # type: ignore[arg-type]
        retention_hours=24,
    )
    return CloudPipeline(
        database=database,
        base_config=_config(tmp_path),
        runner=NoopRunner(),
        delivery=delivery,
        scratch_root=tmp_path / "scratch",
        acquisition_factory=FakeAcquisitionService,
        ableton_factory=FakeAbletonService,
    )


def _outputs(database: CloudDatabase, job_id: int) -> list[dict[str, object]]:
    with database.connect() as connection:
        return list(
            connection.execute(
                "SELECT * FROM outputs WHERE job_id = %s ORDER BY id",
                (job_id,),
            ).fetchall()
        )


def test_source_profile_runs_to_temporary_delivery_and_cleans_workspace(
    cloud_db: CloudDatabase,
    tmp_path: Path,
) -> None:
    job_id = cloud_db.create_job(
        CloudJobRequest(url=VIDEO_URL, profile=CloudProfile.SOURCE, origin="url")
    )
    storage = FakeStorage()
    worker = CloudSequentialWorker(
        database=cloud_db,
        pipeline=_pipeline(cloud_db, tmp_path, storage),
        worker_id="worker-source",
        network_class=WorkerNetworkClass.CLOUD_DATACENTER,
    )

    result = worker.run_next()

    assert result is not None
    assert result.state is ProcessingState.COMPLETED
    job = cloud_db.get_job(job_id)
    assert job["processing_state"] == "completed"
    assert job["delivery_state"] == "available"
    assert job["quality_status"] == "verified_best_available"
    assert [row["role"] for row in _outputs(cloud_db, job_id)] == ["source"]
    assert not (tmp_path / "scratch" / str(job_id)).exists()

    with cloud_db.connect() as connection:
        attempt = connection.execute(
            "SELECT * FROM processing_attempts WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    assert attempt["result"] == "completed"
    assert attempt["ended_at_utc"] is not None
    assert attempt["tool_versions_json"]["yt_dlp"] == "2026.7.4"


def test_ableton_profile_publishes_verified_source_and_wav(
    cloud_db: CloudDatabase,
    tmp_path: Path,
) -> None:
    job_id = cloud_db.create_job(
        CloudJobRequest(url=VIDEO_URL, profile=CloudProfile.ABLETON, origin="url")
    )
    storage = FakeStorage()
    worker = CloudSequentialWorker(
        database=cloud_db,
        pipeline=_pipeline(cloud_db, tmp_path, storage),
        worker_id="worker-ableton",
    )

    result = worker.run_next()

    assert result is not None
    assert result.state is ProcessingState.COMPLETED
    outputs = _outputs(cloud_db, job_id)
    assert [row["role"] for row in outputs] == ["source", "ableton"]
    assert outputs[1]["media_properties_json"]["audio_format"] == "pcm_f32le"
    assert outputs[1]["media_properties_json"]["sample_rate_hz"] == 48000
    assert not (tmp_path / "scratch" / str(job_id)).exists()


def test_publication_failure_rolls_back_unavailable_objects_and_keeps_workspace_for_retry(
    cloud_db: CloudDatabase,
    tmp_path: Path,
) -> None:
    job_id = cloud_db.create_job(
        CloudJobRequest(url=VIDEO_URL, profile=CloudProfile.ABLETON, origin="url")
    )
    storage = FakeStorage(fail_role="ableton")
    worker = CloudSequentialWorker(
        database=cloud_db,
        pipeline=_pipeline(cloud_db, tmp_path, storage),
        worker_id="worker-failing-publish",
    )

    result = worker.run_next()

    assert result is not None
    assert result.state is ProcessingState.FAILED
    assert result.error_class == "delivery_publication"
    job = cloud_db.get_job(job_id)
    assert job["delivery_state"] == "not_published"
    assert job["error_stage"] == "publishing"
    assert _outputs(cloud_db, job_id) == []
    assert storage.objects == {}
    assert storage.deleted
    assert (tmp_path / "scratch" / str(job_id)).exists()


def test_expired_worker_claim_requeues_active_job_for_verified_source_reuse(
    cloud_db: CloudDatabase,
) -> None:
    job_id = cloud_db.create_job(
        CloudJobRequest(url=VIDEO_URL, profile=CloudProfile.SOURCE, origin="url")
    )
    claim = cloud_db.claim_next_job(worker_id="crashed-worker", lease_seconds=30)
    assert claim is not None
    cloud_db.transition_processing(job_id, ProcessingState.DOWNLOADING)
    with cloud_db.connect() as connection:
        connection.execute(
            "UPDATE worker_claims SET lease_expires_at_utc = NOW() - INTERVAL '1 second' WHERE job_id = %s",
            (job_id,),
        )

    recovered = CloudJobRepository(cloud_db).recover_expired_claims()

    assert recovered.requeued_jobs == 1
    assert recovered.cleared_claims == 1
    job = cloud_db.get_job(job_id)
    assert job["processing_state"] == "ready"
    assert job["retry_count"] == 1


def test_lease_keeper_fails_closed_after_claim_is_removed(cloud_db: CloudDatabase) -> None:
    cloud_db.create_job(CloudJobRequest(url=VIDEO_URL, profile=CloudProfile.SOURCE, origin="url"))
    claim = cloud_db.claim_next_job(worker_id="lease-test", lease_seconds=2)
    assert claim is not None

    with pytest.raises(LostWorkerClaim):
        with LeaseKeeper(
            cloud_db,
            claim,
            lease_seconds=2,
            heartbeat_seconds=0.05,
        ) as keeper:
            with cloud_db.connect() as connection:
                connection.execute("DELETE FROM worker_claims WHERE job_id = %s", (claim.job_id,))
            time.sleep(0.12)
            keeper.check()


def test_archive_package_is_store_only_zip_and_preserves_tree(tmp_path: Path) -> None:
    item = tmp_path / VIDEO_ID
    master = item / "master" / f"{VIDEO_ID}.webm"
    wav = item / "intermediates" / "ableton" / f"{VIDEO_ID}.wav"
    manifest = item / "metadata" / "archive.json"
    artwork = item / "artwork" / "source-thumbnail.jpg"
    for path, data in (
        (master, b"source"),
        (wav, b"wav"),
        (manifest, b"{}\n"),
        (artwork, b"jpeg"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    write_sha256sums(
        item,
        [
            Path("master") / master.name,
            Path("intermediates/ableton") / wav.name,
            Path("metadata/archive.json"),
            Path("artwork") / artwork.name,
        ],
    )

    package = build_archive_package(item, tmp_path / "package" / "archive.zip")

    assert package.sha256 == sha256_file(package.path)
    with ZipFile(package.path, "r") as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert f"{VIDEO_ID}-archive/master/{VIDEO_ID}.webm" in names
        assert f"{VIDEO_ID}-archive/intermediates/ableton/{VIDEO_ID}.wav" in names
        assert f"{VIDEO_ID}-archive/checksums/SHA256SUMS" in names
        assert all(info.compress_type == ZIP_STORED for info in archive.infolist())
