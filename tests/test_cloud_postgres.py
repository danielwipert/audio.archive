from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

from audio_archive.cloud.db import CloudDatabase, LostWorkerClaim
from audio_archive.cloud.models import CloudJobRequest, CloudProfile, ProcessingState


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


def test_migrations_are_idempotent(cloud_db: CloudDatabase) -> None:
    root = Path(__file__).resolve().parents[1]
    assert cloud_db.apply_migrations(root / "migrations") == []

    with cloud_db.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [row["version"] for row in versions] == [1, 2]


def test_create_manual_job_starts_pending(cloud_db: CloudDatabase) -> None:
    job_id = cloud_db.create_job(
        CloudJobRequest(artist="Portishead", title="Roads", profile=CloudProfile.ABLETON)
    )

    job = cloud_db.get_job(job_id)
    assert job["processing_state"] == "pending"
    assert job["delivery_state"] == "not_published"
    assert job["source_id"] is None


def test_exact_url_job_is_pinned_and_ready(cloud_db: CloudDatabase) -> None:
    job_id = cloud_db.create_job(
        CloudJobRequest(
            artist="Rick Astley",
            title="Never Gonna Give You Up",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            profile=CloudProfile.SOURCE,
            origin="url",
        )
    )

    job = cloud_db.get_job(job_id)
    assert job["processing_state"] == "ready"
    assert job["source_extractor"] == "youtube"
    assert job["source_id"] == "dQw4w9WgXcQ"
    assert job["source_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert job["resolution_method"] == "exact_url"


def test_ready_job_is_claimed_before_pending_job(cloud_db: CloudDatabase) -> None:
    pending_id = cloud_db.create_job(CloudJobRequest(artist="A", title="Pending"))
    ready_id = cloud_db.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", origin="url")
    )

    first = cloud_db.claim_next_job(worker_id="worker-a")
    second = cloud_db.claim_next_job(worker_id="worker-b")

    assert first is not None
    assert first.job_id == ready_id
    assert first.processing_state is ProcessingState.READY
    assert second is not None
    assert second.job_id == pending_id
    assert second.processing_state is ProcessingState.PENDING


def test_active_claim_prevents_duplicate_claim(cloud_db: CloudDatabase) -> None:
    job_id = cloud_db.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", origin="url")
    )

    first = cloud_db.claim_next_job(worker_id="worker-a")
    second = cloud_db.claim_next_job(worker_id="worker-b")

    assert first is not None
    assert first.job_id == job_id
    assert second is None


def test_expired_claim_can_be_recovered(cloud_db: CloudDatabase) -> None:
    job_id = cloud_db.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", origin="url")
    )
    first = cloud_db.claim_next_job(worker_id="worker-a", lease_seconds=60)
    assert first is not None

    with cloud_db.connect() as connection:
        connection.execute(
            "UPDATE worker_claims SET lease_expires_at_utc = NOW() - INTERVAL '1 second' "
            "WHERE job_id = %s",
            (job_id,),
        )

    recovered = cloud_db.claim_next_job(worker_id="worker-b")
    assert recovered is not None
    assert recovered.job_id == job_id
    assert recovered.worker_id == "worker-b"
    assert recovered.claim_token != first.claim_token


def test_heartbeat_and_release_require_current_claim(cloud_db: CloudDatabase) -> None:
    cloud_db.create_job(CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", origin="url"))
    claim = cloud_db.claim_next_job(worker_id="worker-a")
    assert claim is not None

    renewed = cloud_db.heartbeat_claim(claim, lease_seconds=600)
    assert renewed.lease_expires_at_utc > claim.lease_expires_at_utc

    cloud_db.release_claim(renewed)
    with pytest.raises(LostWorkerClaim):
        cloud_db.heartbeat_claim(renewed)


def test_processing_transition_is_persisted_with_event(cloud_db: CloudDatabase) -> None:
    job_id = cloud_db.create_job(CloudJobRequest(artist="Portishead", title="Roads"))

    cloud_db.transition_processing(job_id, ProcessingState.RESOLVING, message="resolver started")

    job = cloud_db.get_job(job_id)
    assert job["processing_state"] == "resolving"
    assert job["started_at_utc"] is not None

    with cloud_db.connect() as connection:
        events = connection.execute(
            "SELECT event_type, from_processing_state, to_processing_state, message "
            "FROM job_events WHERE job_id = %s ORDER BY id",
            (job_id,),
        ).fetchall()
    assert events[-1]["from_processing_state"] == "pending"
    assert events[-1]["to_processing_state"] == "resolving"
    assert events[-1]["message"] == "resolver started"


def test_invalid_processing_transition_rolls_back(cloud_db: CloudDatabase) -> None:
    job_id = cloud_db.create_job(CloudJobRequest(artist="Portishead", title="Roads"))

    with pytest.raises(ValueError, match="Invalid processing transition"):
        cloud_db.transition_processing(job_id, ProcessingState.COMPLETED)

    assert cloud_db.get_job(job_id)["processing_state"] == "pending"
