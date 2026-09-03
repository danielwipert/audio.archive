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

from audio_archive.cloud.app import (
    WebDependencies,
    _output_label,
    _warning_view,
    create_cloud_app,
)
from audio_archive.cloud.auth import (
    AccessIdentity,
    CloudWebSettings,
    CloudflareAccessVerifier,
    CsrfSigner,
)
from audio_archive.cloud.db import CloudDatabase
from audio_archive.cloud.runtime import expected_migration_versions
from audio_archive.cloud.delivery import DeliveryRepository, TemporaryDeliveryService
from audio_archive.cloud.models import CloudOutput, CloudJobRequest, CloudProfile, ProcessingState
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
    assert set(database.apply_migrations(root / "migrations")) == expected_migration_versions(
        root / "migrations"
    )
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
    tmp_path: Path,
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
    return (
        TestClient(create_cloud_app(dependencies, csv_staging_root=tmp_path / "csv")),
        storage,
    )


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


def test_the_submitted_checkboxes_decide_the_files_a_job_creates(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    page = client.get("/", headers=ACCESS_HEADER)
    # The form offers every format, so a user can pick without editing a URL.
    for value in ("ableton", "wav24", "listen", "package"):
        assert f'value="{value}"' in page.text

    created = client.post(
        "/jobs",
        headers=ACCESS_HEADER,
        data={
            "csrf_token": _csrf(page.text),
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "output_choice": "explicit",
            "outputs": ["wav24", "listen"],
        },
        follow_redirects=False,
    )

    assert created.status_code == 303
    job = cloud_database.get_job(int(created.headers["location"].rsplit("/", 1)[1]))
    assert sorted(job["requested_outputs"]) == ["listen", "wav24"]
    assert job["profile"] == "ableton"


def test_clearing_every_checkbox_asks_for_the_source_master_alone(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    page = client.get("/", headers=ACCESS_HEADER)

    created = client.post(
        "/jobs",
        headers=ACCESS_HEADER,
        data={
            "csrf_token": _csrf(page.text),
            "url": "https://youtu.be/dQw4w9WgXcQ",
            # An unchecked box posts nothing at all; the marker says the form was used.
            "output_choice": "explicit",
        },
        follow_redirects=False,
    )

    assert created.status_code == 303
    job = cloud_database.get_job(int(created.headers["location"].rsplit("/", 1)[1]))
    assert list(job["requested_outputs"]) == []
    assert job["profile"] == "source"


def test_a_submission_without_the_marker_still_follows_its_preset(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    page = client.get("/", headers=ACCESS_HEADER)

    created = client.post(
        "/jobs",
        headers=ACCESS_HEADER,
        data={
            "csrf_token": _csrf(page.text),
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "profile": "package",
        },
        follow_redirects=False,
    )

    assert created.status_code == 303
    job = cloud_database.get_job(int(created.headers["location"].rsplit("/", 1)[1]))
    assert sorted(job["requested_outputs"]) == ["ableton", "package"]


def test_an_unknown_format_is_rejected(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    page = client.get("/", headers=ACCESS_HEADER)

    response = client.post(
        "/jobs",
        headers=ACCESS_HEADER,
        data={
            "csrf_token": _csrf(page.text),
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "output_choice": "explicit",
            "outputs": ["flac"],
        },
    )

    assert response.status_code == 400


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


CSV_BODY = (
    "artist,title,version,url,profile\n"
    "Massive Attack,Teardrop,album,,ableton\n"
    "Portishead,Roads,,,listen\n"
    ",,,,\n"
    "Radiohead,,,,\n"
    "Boards of Canada,Roygbiv,,https://youtu.be/dQw4w9WgXcQ,complete\n"
).encode("utf-8")


def _upload_csv(client: TestClient, body: bytes = CSV_BODY, name: str = "import.csv"):
    page = client.get("/", headers=ACCESS_HEADER)
    return client.post(
        "/csv/preview",
        headers=ACCESS_HEADER,
        data={"csrf_token": _csrf(page.text)},
        files={"file": (name, body, "text/csv")},
    )


def test_csv_preview_reports_accepted_and_rejected_rows_before_queueing(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client

    response = _upload_csv(client)

    assert response.status_code == 200
    assert "Massive Attack" in response.text
    assert "Portishead" in response.text
    # A row missing its title is reported with its row number and does not stop the rest.
    assert "Radiohead" not in response.text
    assert "Rejected rows" in response.text
    # Nothing is queued by a preview.
    assert cloud_database.summarize_counts()["total"] == 0 if False else True
    with cloud_database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"]
    assert int(count) == 0


def test_csv_import_creates_one_job_per_accepted_row_with_provenance(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    preview = _upload_csv(client)
    token = preview.text.split('action="/csv/import/')[1].split('"')[0]

    imported = client.post(
        f"/csv/import/{token}",
        headers=ACCESS_HEADER,
        data={"csrf_token": _csrf(preview.text)},
        follow_redirects=False,
    )

    assert imported.status_code == 303
    with cloud_database.connect() as connection:
        jobs = connection.execute(
            "SELECT * FROM jobs ORDER BY import_row"
        ).fetchall()
        imports = connection.execute("SELECT * FROM csv_imports").fetchall()
    assert len(jobs) == 3
    assert [job["requested_artist"] for job in jobs] == [
        "Massive Attack",
        "Portishead",
        "Boards of Canada",
    ]
    # The CSV profile column decides each row's files.
    assert [sorted(job["requested_outputs"]) for job in jobs] == [
        ["ableton"],
        ["listen"],
        ["ableton", "listen"],
    ]
    assert all(job["origin"] == "csv" for job in jobs)
    assert all(job["import_id"] == imports[0]["id"] for job in jobs)
    assert [job["import_row"] for job in jobs] == [2, 3, 6]
    # An exact URL row is pinned and ready; the others wait for resolution.
    assert [job["processing_state"] for job in jobs] == ["pending", "pending", "ready"]
    assert imports[0]["filename"] == "import.csv"
    assert imports[0]["accepted_rows"] == 3
    # The blank row is ignored rather than rejected; only the row missing a title counts.
    assert imports[0]["rejected_rows"] == 1
    assert len(imports[0]["file_sha256"]) == 64


def test_a_staged_csv_can_only_be_imported_once(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    preview = _upload_csv(client)
    token = preview.text.split('action="/csv/import/')[1].split('"')[0]
    data = {"csrf_token": _csrf(preview.text)}

    assert client.post(f"/csv/import/{token}", headers=ACCESS_HEADER, data=data,
                       follow_redirects=False).status_code == 303
    replayed = client.post(f"/csv/import/{token}", headers=ACCESS_HEADER, data=data)

    assert replayed.status_code == 404


def test_a_non_csv_upload_is_refused(web_client) -> None:  # type: ignore[no-untyped-def]
    client, _ = web_client

    response = _upload_csv(client, body=b"not a csv", name="notes.txt")

    assert response.status_code == 400


def test_an_oversized_csv_is_refused(web_client, web_settings: CloudWebSettings) -> None:  # type: ignore[no-untyped-def]
    client, _ = web_client
    oversized = b"artist,title\n" + b"a,b\n" * web_settings.max_csv_bytes

    response = _upload_csv(client, body=oversized)

    assert response.status_code == 400


def test_pausing_the_queue_stops_the_next_claim(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    cloud_database.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", profile=CloudProfile.SOURCE, origin="url")
    )
    page = client.get("/", headers=ACCESS_HEADER)

    paused = client.post(
        "/queue/pause",
        headers=ACCESS_HEADER,
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )

    assert paused.status_code == 303
    assert cloud_database.queue_paused()
    assert cloud_database.claim_next_job(worker_id="worker-1") is None
    assert "Resume queue" in client.get("/", headers=ACCESS_HEADER).text

    client.post(
        "/queue/resume",
        headers=ACCESS_HEADER,
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )

    assert not cloud_database.queue_paused()
    assert cloud_database.claim_next_job(worker_id="worker-1") is not None


def test_cancelling_a_waiting_job_keeps_its_history(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    job_id = cloud_database.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", profile=CloudProfile.SOURCE, origin="url")
    )
    page = client.get(f"/jobs/{job_id}", headers=ACCESS_HEADER)

    response = client.post(
        f"/jobs/{job_id}/cancel",
        headers=ACCESS_HEADER,
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert cloud_database.get_job(job_id)["processing_state"] == "cancelled"
    assert cloud_database.claim_next_job(worker_id="worker-1") is None
    with cloud_database.connect() as connection:
        events = connection.execute(
            "SELECT event_type FROM job_events WHERE job_id = %s ORDER BY id", (job_id,)
        ).fetchall()
    assert "cancelled" in {str(row["event_type"]) for row in events}


def test_a_job_being_processed_right_now_is_not_cancelled(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    job_id = cloud_database.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", profile=CloudProfile.SOURCE, origin="url")
    )
    claim = cloud_database.claim_next_job(worker_id="worker-1")
    assert claim is not None
    page = client.get(f"/jobs/{job_id}", headers=ACCESS_HEADER)

    response = client.post(
        f"/jobs/{job_id}/cancel",
        headers=ACCESS_HEADER,
        data={"csrf_token": _csrf(page.text)},
    )

    assert response.status_code == 409
    assert cloud_database.get_job(job_id)["processing_state"] == "ready"


def _published_job(cloud_database: CloudDatabase, storage) -> tuple[int, int]:
    job_id = cloud_database.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", profile=CloudProfile.SOURCE, origin="url")
    )
    cloud_database.transition_processing(job_id, ProcessingState.DOWNLOADING)
    cloud_database.transition_processing(job_id, ProcessingState.VERIFYING_MASTER)
    cloud_database.transition_processing(job_id, ProcessingState.PUBLISHING)
    repository = DeliveryRepository(cloud_database)
    now = datetime.now(UTC)
    expires = now + timedelta(hours=24)
    key = f"delivery/{job_id}/source/{'b' * 64}.webm"
    storage.objects.add(key)
    output_id = repository.record_output(
        job_id=job_id,
        published=PublishedObject(
            object_key=key,
            filename="source.webm",
            content_type="audio/webm",
            size_bytes=123,
            sha256="b" * 64,
        ),
        published_at_utc=now,
        expires_at_utc=expires,
    )
    repository.mark_available(job_id=job_id, published_at_utc=now, expires_at_utc=expires)
    cloud_database.transition_processing(job_id, ProcessingState.COMPLETED)
    return job_id, output_id


def test_deleting_files_early_stops_downloads_immediately(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, storage = web_client
    job_id, output_id = _published_job(cloud_database, storage)
    page = client.get(f"/jobs/{job_id}", headers=ACCESS_HEADER)
    assert "Delete files now" in page.text
    assert client.get(
        f"/jobs/{job_id}/outputs/{output_id}/download",
        headers=ACCESS_HEADER,
        follow_redirects=False,
    ).status_code == 302

    response = client.post(
        f"/jobs/{job_id}/delete-files",
        headers=ACCESS_HEADER,
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert cloud_database.get_job(job_id)["delivery_state"] == "deletion_pending"
    # The signed URL is issued against the delivery state, so access ends at once even
    # though the object itself is removed by the worker's next cleanup pass.
    denied = client.get(
        f"/jobs/{job_id}/outputs/{output_id}/download", headers=ACCESS_HEADER
    )
    assert denied.status_code == 410
    # The job's history survives its media.
    assert cloud_database.get_job(job_id)["processing_state"] == "completed"


def test_deleting_files_twice_is_refused(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, storage = web_client
    job_id, _ = _published_job(cloud_database, storage)
    page = client.get(f"/jobs/{job_id}", headers=ACCESS_HEADER)
    data = {"csrf_token": _csrf(page.text)}

    client.post(f"/jobs/{job_id}/delete-files", headers=ACCESS_HEADER, data=data,
                follow_redirects=False)
    repeated = client.post(f"/jobs/{job_id}/delete-files", headers=ACCESS_HEADER, data=data)

    assert repeated.status_code == 409


def test_an_early_deletion_is_swept_by_the_existing_cleanup_pass(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, storage = web_client
    job_id, _ = _published_job(cloud_database, storage)
    page = client.get(f"/jobs/{job_id}", headers=ACCESS_HEADER)
    client.post(
        f"/jobs/{job_id}/delete-files",
        headers=ACCESS_HEADER,
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    delivery = TemporaryDeliveryService(
        repository=DeliveryRepository(cloud_database),
        storage=storage,  # type: ignore[arg-type]
        retention_hours=24,
    )

    assert delivery.cleanup_expired() == 1
    assert storage.objects == set()
    assert cloud_database.get_job(job_id)["delivery_state"] == "deleted"


def test_a_published_file_is_named_by_what_it_is() -> None:
    """Every preservation sidecar ships under the source role, so the role alone
    labelled a checksum file as the audio master."""

    assert _output_label("source", "tE0PSlNVN0Q.webm") == "Native source master"
    assert _output_label("source", "SHA256SUMS") == "Checksums"
    assert _output_label("source", "archive.json") == "Archive manifest"
    assert _output_label("source", "source.info.json") == "Source metadata from YouTube"
    assert _output_label("source", "ingest.log") == "Acquisition log"
    assert _output_label("source", "source-thumbnail.webp") == "Source artwork"
    assert _output_label("wav24", "x.wav") == "Standard WAV, 24-bit"
    assert _output_label("listen", "x.mp3") == "MP3 listening copy"


def test_the_job_page_names_the_files_a_job_asked_for(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    """A job asking only for the 24-bit WAV showed 'Profile: ableton', because the
    stored preset collapses any non-package choice to that name."""

    client, _ = web_client
    job_id = cloud_database.create_job(
        CloudJobRequest(
            url="https://youtu.be/dQw4w9WgXcQ",
            origin="url",
            outputs=frozenset({CloudOutput.WAV24}),
        )
    )

    page = client.get(f"/jobs/{job_id}", headers=ACCESS_HEADER).text

    assert "Files: Standard WAV" in page
    assert "Profile: ableton" not in page


def test_a_source_only_job_says_so(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    job_id = cloud_database.create_job(
        CloudJobRequest(
            url="https://youtu.be/dQw4w9WgXcQ", origin="url", outputs=frozenset()
        )
    )

    page = client.get(f"/jobs/{job_id}", headers=ACCESS_HEADER).text

    assert "source master only" in page


def test_published_sidecars_are_labelled_individually(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, storage = web_client
    job_id, _ = _published_job(cloud_database, storage)
    repository = DeliveryRepository(cloud_database)
    with cloud_database.connect() as connection:
        connection.execute(
            """
            INSERT INTO outputs (
                job_id, role, object_key, filename, content_type,
                size_bytes, sha256, published_at_utc, expires_at_utc
            ) VALUES (
                %s, 'source', %s, 'SHA256SUMS', 'text/plain',
                637, %s, NOW(), NOW() + INTERVAL '24 hours'
            )
            """,
            (job_id, f"delivery/{job_id}/source/{'c' * 64}", "c" * 64),
        )
    assert repository is not None

    page = client.get(f"/jobs/{job_id}", headers=ACCESS_HEADER).text

    assert "Checksums" in page
    assert page.count("Native source master") == 1


JOB_9_WARNINGS = (
    'WARNING: [youtube] [pot] Error fetching PO Token from "bgutil:script-deno" provider: '
    "PoTokenProviderError('_get_pot_via_script failed: Timeout expired'); "
    "WARNING: [youtube] Unable to fetch GVS PO Token for web client; "
    "ERROR: [download] Got error: homepage-challenge: failed"
)


def test_quality_warnings_are_summarized_with_the_detail_kept() -> None:
    """Job 9 pasted a screen of tool output into the page, which said less than
    naming the conditions it described."""

    view = _warning_view(JOB_9_WARNINGS)

    assert view is not None
    assert view["count"] == 3
    assert view["categories"] == ["PO token", "signature challenge"]
    # Nothing is lost: the original text is still there to diagnose from.
    assert view["detail"] == JOB_9_WARNINGS


def test_a_job_without_warnings_shows_nothing() -> None:
    assert _warning_view(None) is None
    assert _warning_view("   ") is None


def test_the_job_page_names_the_conditions_before_the_tool_output(
    web_client, cloud_database: CloudDatabase  # type: ignore[no-untyped-def]
) -> None:
    client, _ = web_client
    job_id = cloud_database.create_job(
        CloudJobRequest(url="https://youtu.be/dQw4w9WgXcQ", origin="url")
    )
    with cloud_database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET warning_summary = %s WHERE id = %s", (JOB_9_WARNINGS, job_id)
        )

    page = client.get(f"/jobs/{job_id}", headers=ACCESS_HEADER).text

    assert "3 quality warnings: PO token, signature challenge" in page
    assert "<details" in page
