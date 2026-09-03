from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from audio_archive.cloud.db import CloudDatabase
from audio_archive.cloud.runtime import expected_migration_versions
from audio_archive.cloud.delivery import DeliveryRepository, DeliveryUnavailable, TemporaryDeliveryService
from audio_archive.cloud.models import CloudJobRequest, DeliveryState, ProcessingState
from audio_archive.cloud.storage import PublishedObject


class FakeDeliveryStorage:
    def __init__(self) -> None:
        self.objects: set[str] = set()
        self.deleted: list[str] = []
        self.signed: list[tuple[str, str]] = []

    def publish_file(self, **_: object) -> PublishedObject:
        raise AssertionError("publish_file is not used by repository lifecycle tests")

    def create_download_url(self, *, object_key: str, filename: str) -> str:
        self.signed.append((object_key, filename))
        return f"https://signed.example/{object_key}"

    def object_exists(self, object_key: str) -> bool:
        return object_key in self.objects

    def delete_object(self, object_key: str) -> None:
        self.objects.discard(object_key)
        self.deleted.append(object_key)


@pytest.fixture
def delivery_db() -> CloudDatabase:
    dsn = os.getenv("CLOUD_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("CLOUD_TEST_DATABASE_URL is not configured")

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")

    database = CloudDatabase(dsn)
    root = Path(__file__).resolve().parents[1]
    assert set(database.apply_migrations(root / "migrations")) == expected_migration_versions(
        root / "migrations"
    )
    return database


def _publishing_source_job(database: CloudDatabase) -> int:
    job_id = database.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", origin="url")
    )
    database.transition_processing(job_id, ProcessingState.DOWNLOADING)
    database.transition_processing(job_id, ProcessingState.VERIFYING_MASTER)
    database.transition_processing(job_id, ProcessingState.PUBLISHING)
    return job_id


def _published_object(job_id: int) -> PublishedObject:
    digest = "a" * 64
    return PublishedObject(
        object_key=f"delivery/{job_id}/source/{digest}.webm",
        filename="source.webm",
        content_type="audio/webm",
        size_bytes=1234,
        sha256=digest,
    )


def _move_publication_into_past(
    database: CloudDatabase,
    *,
    job_id: int,
    output_id: int,
) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET published_at_utc = NOW() - INTERVAL '2 days',
                expires_at_utc = NOW() - INTERVAL '1 day'
            WHERE id = %s
            """,
            (job_id,),
        )
        connection.execute(
            """
            UPDATE outputs
            SET published_at_utc = NOW() - INTERVAL '2 days',
                expires_at_utc = NOW() - INTERVAL '1 day'
            WHERE id = %s
            """,
            (output_id,),
        )


def test_output_cannot_be_recorded_before_publishing(delivery_db: CloudDatabase) -> None:
    job_id = delivery_db.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", origin="url")
    )
    repository = DeliveryRepository(delivery_db)
    now = datetime.now(UTC)

    with pytest.raises(ValueError, match="only be recorded while the job is publishing"):
        repository.record_output(
            job_id=job_id,
            published=_published_object(job_id),
            published_at_utc=now,
            expires_at_utc=now + timedelta(hours=24),
        )


def test_publication_requires_at_least_one_recorded_output(delivery_db: CloudDatabase) -> None:
    job_id = _publishing_source_job(delivery_db)
    repository = DeliveryRepository(delivery_db)
    now = datetime.now(UTC)

    with pytest.raises(ValueError, match="At least one verified output"):
        repository.mark_available(
            job_id=job_id,
            published_at_utc=now,
            expires_at_utc=now + timedelta(hours=24),
        )


def test_successful_publication_is_downloadable_only_after_processing_completes(
    delivery_db: CloudDatabase,
) -> None:
    job_id = _publishing_source_job(delivery_db)
    repository = DeliveryRepository(delivery_db)
    storage = FakeDeliveryStorage()
    service = TemporaryDeliveryService(
        repository=repository,
        storage=storage,  # type: ignore[arg-type]
    )
    now = datetime.now(UTC)
    expires = now + timedelta(hours=24)
    published = _published_object(job_id)
    storage.objects.add(published.object_key)

    output_id = repository.record_output(
        job_id=job_id,
        published=published,
        published_at_utc=now,
        expires_at_utc=expires,
        media_properties={"codec": "opus", "sample_rate_hz": 48000},
    )
    repository.mark_available(
        job_id=job_id,
        published_at_utc=now,
        expires_at_utc=expires,
    )

    with pytest.raises(DeliveryUnavailable):
        service.download_url(job_id=job_id, output_id=output_id)

    delivery_db.transition_processing(job_id, ProcessingState.COMPLETED)
    url = service.download_url(job_id=job_id, output_id=output_id)

    assert url == f"https://signed.example/{published.object_key}"
    assert storage.signed == [(published.object_key, "source.webm")]
    job = delivery_db.get_job(job_id)
    assert job["processing_state"] == "completed"
    assert job["delivery_state"] == "available"
    assert job["expires_at_utc"] == expires


def test_expired_delivery_stops_download_before_object_cleanup(delivery_db: CloudDatabase) -> None:
    job_id = _publishing_source_job(delivery_db)
    repository = DeliveryRepository(delivery_db)
    now = datetime.now(UTC)
    expires = now + timedelta(hours=24)
    output_id = repository.record_output(
        job_id=job_id,
        published=_published_object(job_id),
        published_at_utc=now,
        expires_at_utc=expires,
    )
    repository.mark_available(
        job_id=job_id,
        published_at_utc=now,
        expires_at_utc=expires,
    )
    delivery_db.transition_processing(job_id, ProcessingState.COMPLETED)
    _move_publication_into_past(delivery_db, job_id=job_id, output_id=output_id)

    with pytest.raises(DeliveryUnavailable):
        repository.get_downloadable_output(job_id=job_id, output_id=output_id)

    expired = repository.expire_due_jobs()
    assert expired == [job_id]
    assert delivery_db.get_job(job_id)["delivery_state"] == "expired"


def test_cleanup_marks_job_deleted_after_objects_are_gone(delivery_db: CloudDatabase) -> None:
    job_id = _publishing_source_job(delivery_db)
    repository = DeliveryRepository(delivery_db)
    storage = FakeDeliveryStorage()
    service = TemporaryDeliveryService(
        repository=repository,
        storage=storage,  # type: ignore[arg-type]
    )
    now = datetime.now(UTC)
    expires = now + timedelta(hours=24)
    published = _published_object(job_id)
    storage.objects.add(published.object_key)

    output_id = repository.record_output(
        job_id=job_id,
        published=published,
        published_at_utc=now,
        expires_at_utc=expires,
    )
    repository.mark_available(
        job_id=job_id,
        published_at_utc=now,
        expires_at_utc=expires,
    )
    delivery_db.transition_processing(job_id, ProcessingState.COMPLETED)
    _move_publication_into_past(delivery_db, job_id=job_id, output_id=output_id)

    assert service.cleanup_expired() == 1
    assert published.object_key in storage.deleted
    assert delivery_db.get_job(job_id)["delivery_state"] == DeliveryState.DELETED.value

    with delivery_db.connect() as connection:
        output = connection.execute(
            "SELECT deleted_at_utc FROM outputs WHERE id = %s",
            (output_id,),
        ).fetchone()
    assert output["deleted_at_utc"] is not None


def test_database_expiry_is_authoritative_even_if_storage_object_still_exists(
    delivery_db: CloudDatabase,
) -> None:
    job_id = _publishing_source_job(delivery_db)
    repository = DeliveryRepository(delivery_db)
    now = datetime.now(UTC)
    expires = now + timedelta(hours=24)
    output_id = repository.record_output(
        job_id=job_id,
        published=_published_object(job_id),
        published_at_utc=now,
        expires_at_utc=expires,
    )
    repository.mark_available(
        job_id=job_id,
        published_at_utc=now,
        expires_at_utc=expires,
    )
    delivery_db.transition_processing(job_id, ProcessingState.COMPLETED)

    with delivery_db.connect() as connection:
        connection.execute(
            "UPDATE jobs SET expires_at_utc = NOW() - INTERVAL '1 second' WHERE id = %s",
            (job_id,),
        )

    with pytest.raises(DeliveryUnavailable):
        repository.get_downloadable_output(job_id=job_id, output_id=output_id)
