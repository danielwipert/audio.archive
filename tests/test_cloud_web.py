from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from audio_archive.cloud.app import WebDependencies, create_cloud_app
from audio_archive.cloud.auth import (
    AccessIdentity,
    CloudWebSettings,
    CloudflareAccessVerifier,
    CsrfSigner,
)
from audio_archive.cloud.db import CloudDatabase
from audio_archive.cloud.delivery import DeliveryRepository, TemporaryDeliveryService
from audio_archive.cloud.models import CloudJobRequest, CloudProfile, ProcessingState
from audio_archive.cloud.storage import PublishedObject
from audio_archive.cloud.web_repository import CloudWebRepository


ACCESS_HEADER = {"Cf-Access-Jwt-Assertion": "test-access-token"}
IDENTITY = AccessIdentity(subject="subject-1", email="archive@example.com")


class FakeVerifier:
    def verify(self, assertion: str) -> AccessIdentity:
        if assertion != "test-access-token":
            raise PermissionError("invalid")
        return IDENTITY


class FakeStorage:
    def __init__(self) -> None:
        self.objects: set[str] = set()
        self.signed: list[tuple[str, str]] = []

    def create_download_url(self, *, object_key: str, filename: str) -> str:
        self.signed.append((object_key, filename))
        return "https://signed.example/private-download"

    def object_exists(self, object_key: str) -> bool:
        return object_key in self.objects

    def delete_object(self, object_key: str) -> None:
        self.objects.discard(object_key)


@pytest.fixture
def cloud_database() -> CloudDatabase:
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
def web_settings(cloud_database: CloudDatabase) -> CloudWebSettings:
    return CloudWebSettings(
        database_url=cloud_database.dsn,
        r2_endpoint_url="https://example.r2.cloudflarestorage.com",
        r2_bucket="delivery",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        access_team_domain="https://team.cloudflareaccess.com",
        access_audience="audience",
        csrf_secret="c" * 64,
        allowed_emails=frozenset({IDENTITY.email}),
    )


@pytest.fixture
def web_client(
    cloud_database: CloudDatabase,
    web_settings: CloudWebSettings,
) -> tuple[TestClient, FakeStorage]:
    storage = FakeStorage()
    delivery = TemporaryDeliveryService(
        repository=DeliveryRepository(cloud_database),
        storage=storage,  # type: ignore[arg-type]
        retention_hours=24,
    )
    dependencies = WebDependencies(
        settings=web_settings,
        database=cloud_database,
        delivery=delivery,
        verifier=FakeVerifier(),
    )
    return TestClient(create_cloud_app(dependencies)), storage


def _csrf(response_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def test_health_is_public_but_application_requires_access(web_client) -> None:  # type: ignore[no-untyped-def]
    client, _ = web_client

    assert client.get("/healthz").status_code == 200
    denied = client.get("/")
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"

    allowed = client.get("/", headers=ACCESS_HEADER)
    assert allowed.status_code == 200
    assert "archive@example.com" in allowed.text
    assert allowed.headers["x-frame-options"] == "DENY"


def test_create_exact_url_job_is_ready_and_csrf_protected(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    page = client.get("/", headers=ACCESS_HEADER)
    token = _csrf(page.text)

    rejected = client.post(
        "/jobs",
        headers=ACCESS_HEADER,
        data={"csrf_token": "bad", "url": "https://youtu.be/dQw4w9WgXcQ", "profile": "source"},
    )
    assert rejected.status_code == 403

    created = client.post(
        "/jobs",
        headers=ACCESS_HEADER,
        data={
            "csrf_token": token,
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "profile": "source",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    job_id = int(created.headers["location"].rsplit("/", 1)[1])
    job = cloud_database.get_job(job_id)
    assert job["processing_state"] == "ready"
    assert job["source_id"] == "dQw4w9WgXcQ"
    assert job["profile"] == "source"


def test_artist_title_submission_creates_pending_job(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    token = _csrf(client.get("/", headers=ACCESS_HEADER).text)

    response = client.post(
        "/jobs",
        headers=ACCESS_HEADER,
        data={
            "csrf_token": token,
            "artist": "Portishead",
            "title": "Roads",
            "version": "album",
            "profile": "ableton",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    job_id = int(response.headers["location"].rsplit("/", 1)[1])
    job = cloud_database.get_job(job_id)
    assert job["processing_state"] == "pending"
    assert job["requested_artist"] == "Portishead"
    assert job["requested_title"] == "Roads"


def test_candidate_review_approves_only_recorded_candidate(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    job_id = cloud_database.create_job(
        CloudJobRequest(artist="Portishead", title="Roads", profile=CloudProfile.ABLETON)
    )
    cloud_database.transition_processing(job_id, ProcessingState.RESOLVING)
    with cloud_database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET processing_state = 'needs_review' WHERE id = %s",
            (job_id,),
        )
        connection.execute(
            """
            INSERT INTO candidates (
                job_id, position, video_id, url, title, channel, score,
                reasons_json, warnings_json, disqualified
            ) VALUES (%s, 1, 'dQw4w9WgXcQ', %s, 'Roads - Official Audio', 'Portishead', 89,
                      '[\"title match\"]'::jsonb, '[]'::jsonb, FALSE)
            """,
            (job_id, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        )
    page = client.get(f"/jobs/{job_id}", headers=ACCESS_HEADER)
    token = _csrf(page.text)

    response = client.post(
        f"/jobs/{job_id}/approve/dQw4w9WgXcQ",
        headers=ACCESS_HEADER,
        data={"csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    job = cloud_database.get_job(job_id)
    assert job["processing_state"] == "ready"
    assert job["resolution_method"] == "manual_selection"
    assert job["source_id"] == "dQw4w9WgXcQ"


def test_download_redirect_requires_successful_unexpired_delivery(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, storage = web_client
    job_id = cloud_database.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", profile=CloudProfile.SOURCE, origin="url")
    )
    cloud_database.transition_processing(job_id, ProcessingState.DOWNLOADING)
    cloud_database.transition_processing(job_id, ProcessingState.VERIFYING_MASTER)
    cloud_database.transition_processing(job_id, ProcessingState.PUBLISHING)
    repository = DeliveryRepository(cloud_database)
    now = datetime.now(UTC)
    expires = now + timedelta(hours=24)
    key = f"delivery/{job_id}/source/{'a' * 64}.webm"
    storage.objects.add(key)
    output_id = repository.record_output(
        job_id=job_id,
        published=PublishedObject(
            object_key=key,
            filename="source.webm",
            content_type="audio/webm",
            size_bytes=123,
            sha256="a" * 64,
        ),
        published_at_utc=now,
        expires_at_utc=expires,
    )
    repository.mark_available(job_id=job_id, published_at_utc=now, expires_at_utc=expires)
    cloud_database.transition_processing(job_id, ProcessingState.COMPLETED)

    denied = client.get(
        f"/jobs/{job_id}/outputs/{output_id}/download",
        follow_redirects=False,
    )
    assert denied.status_code == 403

    allowed = client.get(
        f"/jobs/{job_id}/outputs/{output_id}/download",
        headers=ACCESS_HEADER,
        follow_redirects=False,
    )
    assert allowed.status_code == 302
    assert allowed.headers["location"] == "https://signed.example/private-download"
    assert storage.signed == [(key, "source.webm")]


def test_retry_clears_failure_and_requeues_pinned_source(cloud_database: CloudDatabase) -> None:
    job_id = cloud_database.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", profile=CloudProfile.SOURCE, origin="url")
    )
    cloud_database.transition_processing(job_id, ProcessingState.FAILED)
    with cloud_database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET error_stage='downloading', error_class='youtube_access_403', error_summary='403' WHERE id=%s",
            (job_id,),
        )

    state = CloudWebRepository(cloud_database).retry_job(job_id)

    assert state is ProcessingState.READY
    job = cloud_database.get_job(job_id)
    assert job["retry_count"] == 1
    assert job["error_summary"] is None


def test_manual_retry_clears_the_automatic_backoff(cloud_database: CloudDatabase) -> None:
    job_id = cloud_database.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", profile=CloudProfile.SOURCE, origin="url")
    )
    cloud_database.transition_processing(job_id, ProcessingState.FAILED)
    with cloud_database.connect() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET access_retry_count = 3,
                retry_not_before_utc = NOW() + INTERVAL '20 minutes'
            WHERE id = %s
            """,
            (job_id,),
        )

    CloudWebRepository(cloud_database).retry_job(job_id)

    job = cloud_database.get_job(job_id)
    assert job["retry_not_before_utc"] is None
    assert job["access_retry_count"] == 0
    assert cloud_database.claim_next_job(worker_id="worker-1") is not None


def test_csrf_is_bound_to_identity() -> None:
    signer = CsrfSigner("s" * 64, max_age_seconds=60)
    token = signer.issue(IDENTITY)
    signer.verify(token, IDENTITY)
    with pytest.raises(PermissionError):
        signer.verify(token, AccessIdentity(subject="other", email="archive@example.com"))


def test_cloudflare_verifier_checks_signature_audience_issuer_and_email(
    web_settings: CloudWebSettings,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "aud": [web_settings.access_audience],
        "email": IDENTITY.email,
        "exp": now + 300,
        "iat": now,
        "nbf": now - 1,
        "iss": web_settings.access_team_domain,
        "type": "app",
        "sub": IDENTITY.subject,
    }
    token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "test"})
    verifier = CloudflareAccessVerifier(web_settings)
    verifier._jwks = SimpleNamespace(  # type: ignore[assignment]
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=public_key)
    )

    assert verifier.verify(token) == IDENTITY

    wrong = dict(claims)
    wrong["aud"] = ["another-app"]
    wrong_token = jwt.encode(wrong, private_pem, algorithm="RS256", headers={"kid": "test"})
    with pytest.raises(PermissionError):
        verifier.verify(wrong_token)
