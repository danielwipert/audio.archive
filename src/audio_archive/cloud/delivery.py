from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .db import CloudDatabase
from .models import DeliveryState, ProcessingState, ensure_delivery_transition
from .storage import PublishedObject, R2DeliveryStorage


class DeliveryUnavailable(RuntimeError):
    """Raised when a temporary output is not currently eligible for download."""


@dataclass(frozen=True)
class OutputRecord:
    id: int
    job_id: int
    role: str
    object_key: str
    filename: str
    content_type: str | None
    size_bytes: int
    sha256: str
    expires_at_utc: datetime


class DeliveryRepository:
    def __init__(self, database: CloudDatabase):
        self.database = database

    def record_output(
        self,
        *,
        job_id: int,
        published: PublishedObject,
        published_at_utc: datetime,
        expires_at_utc: datetime,
        media_properties: dict[str, object] | None = None,
    ) -> int:
        published_at = _aware_utc(published_at_utc)
        expires_at = _aware_utc(expires_at_utc)
        if expires_at <= published_at:
            raise ValueError("expires_at_utc must be after published_at_utc")

        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT processing_state, delivery_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            if job["processing_state"] != ProcessingState.PUBLISHING.value:
                raise ValueError("Outputs may only be recorded while the job is publishing")
            if job["delivery_state"] != DeliveryState.NOT_PUBLISHED.value:
                raise ValueError("Outputs may not be added after delivery is published")

            row = connection.execute(
                """
                INSERT INTO outputs (
                    job_id, role, object_key, filename, content_type,
                    size_bytes, sha256, media_properties_json,
                    published_at_utc, expires_at_utc
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (job_id, role, object_key) DO UPDATE
                SET filename = EXCLUDED.filename,
                    content_type = EXCLUDED.content_type,
                    size_bytes = EXCLUDED.size_bytes,
                    sha256 = EXCLUDED.sha256,
                    media_properties_json = EXCLUDED.media_properties_json,
                    published_at_utc = EXCLUDED.published_at_utc,
                    expires_at_utc = EXCLUDED.expires_at_utc,
                    deleted_at_utc = NULL
                RETURNING id
                """,
                (
                    job_id,
                    _validate_role(published.object_key, published),
                    published.object_key,
                    published.filename,
                    published.content_type,
                    published.size_bytes,
                    published.sha256,
                    json.dumps(media_properties or {}, sort_keys=True),
                    published_at,
                    expires_at,
                ),
            ).fetchone()
            assert row is not None
            return int(row["id"])

    def mark_available(
        self,
        *,
        job_id: int,
        published_at_utc: datetime,
        expires_at_utc: datetime,
    ) -> None:
        published_at = _aware_utc(published_at_utc)
        expires_at = _aware_utc(expires_at_utc)
        if expires_at <= published_at:
            raise ValueError("expires_at_utc must be after published_at_utc")

        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT processing_state, delivery_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            if job["processing_state"] != ProcessingState.PUBLISHING.value:
                raise ValueError("Job must be in publishing state before delivery is made available")
            if job["delivery_state"] != DeliveryState.NOT_PUBLISHED.value:
                raise ValueError("Job delivery is already published or expired")

            output_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM outputs
                    WHERE job_id = %s
                      AND deleted_at_utc IS NULL
                      AND expires_at_utc = %s
                    """,
                    (job_id, expires_at),
                ).fetchone()["count"]
            )
            if output_count <= 0:
                raise ValueError("At least one verified output must be recorded before publication")

            connection.execute(
                """
                UPDATE jobs
                SET delivery_state = 'available',
                    published_at_utc = %s,
                    expires_at_utc = %s,
                    updated_at_utc = NOW()
                WHERE id = %s
                """,
                (published_at, expires_at, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_delivery_state, to_delivery_state,
                    event_type, message, detail_json
                ) VALUES (
                    %s, 'not_published', 'available', 'delivery_published',
                    'Verified outputs published to temporary delivery storage',
                    jsonb_build_object('expires_at_utc', %s::timestamptz)
                )
                """,
                (job_id, expires_at),
            )

    def get_downloadable_output(self, *, job_id: int, output_id: int) -> OutputRecord:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT o.*
                FROM outputs AS o
                JOIN jobs AS j ON j.id = o.job_id
                WHERE o.id = %s
                  AND o.job_id = %s
                  AND o.deleted_at_utc IS NULL
                  AND o.expires_at_utc > NOW()
                  AND j.delivery_state = 'available'
                  AND j.expires_at_utc > NOW()
                  AND j.processing_state IN (
                      'completed', 'completed_with_warnings', 'skipped_duplicate'
                  )
                """,
                (output_id, job_id),
            ).fetchone()
        if row is None:
            raise DeliveryUnavailable("Output is not currently available for download")
        return _output_record(row)

    def expire_due_jobs(self, *, limit: int = 100) -> list[int]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                WITH due AS (
                    SELECT id
                    FROM jobs
                    WHERE delivery_state = 'available'
                      AND expires_at_utc <= NOW()
                    ORDER BY expires_at_utc, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                ), updated AS (
                    UPDATE jobs AS j
                    SET delivery_state = 'expired', updated_at_utc = NOW()
                    FROM due
                    WHERE j.id = due.id
                    RETURNING j.id
                )
                SELECT id FROM updated ORDER BY id
                """,
                (limit,),
            ).fetchall()
            job_ids = [int(row["id"]) for row in rows]
            for job_id in job_ids:
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, from_delivery_state, to_delivery_state,
                        event_type, message
                    ) VALUES (
                        %s, 'available', 'expired', 'delivery_expired',
                        'Temporary delivery window expired'
                    )
                    """,
                    (job_id,),
                )
            return job_ids

    def request_early_deletion(self, job_id: int) -> None:
        """Stop serving a job's files now and queue its objects for removal.

        Downloads stop the moment the delivery leaves `available`, because every signed
        URL is issued against that state. The objects themselves are removed by the
        worker's next cleanup pass, with the storage lifecycle rule as the backstop.
        """

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT delivery_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            old_state = DeliveryState(str(row["delivery_state"]))
            if old_state is not DeliveryState.AVAILABLE:
                raise ValueError("Only an available delivery can be deleted early")
            ensure_delivery_transition(old_state, DeliveryState.DELETION_PENDING)
            connection.execute(
                """
                UPDATE jobs
                SET delivery_state = 'deletion_pending',
                    deletion_requested_at_utc = NOW(),
                    updated_at_utc = NOW()
                WHERE id = %s
                """,
                (job_id,),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_delivery_state, to_delivery_state,
                    event_type, message
                ) VALUES (
                    %s, 'available', 'deletion_pending', 'delivery_deletion_requested',
                    'User deleted the temporary files before they expired'
                )
                """,
                (job_id,),
            )

    def list_cleanup_outputs(self, *, limit: int = 100) -> list[OutputRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT o.*
                FROM outputs AS o
                JOIN jobs AS j ON j.id = o.job_id
                WHERE o.deleted_at_utc IS NULL
                  AND (
                      j.delivery_state IN ('expired', 'deletion_pending')
                      OR o.expires_at_utc <= NOW()
                  )
                ORDER BY o.expires_at_utc, o.id
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [_output_record(row) for row in rows]

    def mark_output_deleted(self, output_id: int) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                UPDATE outputs
                SET deleted_at_utc = COALESCE(deleted_at_utc, NOW())
                WHERE id = %s
                RETURNING job_id
                """,
                (output_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Output {output_id} does not exist")
            return int(row["job_id"])

    def mark_job_deleted_if_empty(self, job_id: int) -> bool:
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT delivery_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            if job["delivery_state"] not in {
                DeliveryState.EXPIRED.value,
                DeliveryState.DELETION_PENDING.value,
            }:
                return False
            remaining = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM outputs WHERE job_id = %s AND deleted_at_utc IS NULL",
                    (job_id,),
                ).fetchone()["count"]
            )
            if remaining:
                return False

            old_state = str(job["delivery_state"])
            connection.execute(
                """
                UPDATE jobs
                SET delivery_state = 'deleted', deleted_at_utc = NOW(), updated_at_utc = NOW()
                WHERE id = %s
                """,
                (job_id,),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_delivery_state, to_delivery_state,
                    event_type, message
                ) VALUES (%s, %s, 'deleted', 'delivery_deleted', 'Temporary objects deleted')
                """,
                (job_id, old_state),
            )
            return True


class TemporaryDeliveryService:
    def __init__(
        self,
        *,
        repository: DeliveryRepository,
        storage: R2DeliveryStorage,
        retention_hours: int = 24,
    ) -> None:
        if retention_hours <= 0:
            raise ValueError("retention_hours must be positive")
        self.repository = repository
        self.storage = storage
        self.retention_hours = retention_hours

    def publish_file(
        self,
        *,
        job_id: int,
        role: str,
        path: Path,
        filename: str,
        content_type: str,
        expected_sha256: str,
        published_at_utc: datetime,
        expires_at_utc: datetime,
        media_properties: dict[str, object] | None = None,
    ) -> int:
        published = self.storage.publish_file(
            job_id=job_id,
            role=role,
            path=path,
            filename=filename,
            content_type=content_type,
            expected_sha256=expected_sha256,
        )
        try:
            return self.repository.record_output(
                job_id=job_id,
                published=published,
                published_at_utc=published_at_utc,
                expires_at_utc=expires_at_utc,
                media_properties=media_properties,
            )
        except Exception:
            self.storage.delete_object(published.object_key)
            raise

    def default_expiry(self, published_at_utc: datetime) -> datetime:
        return _aware_utc(published_at_utc) + timedelta(hours=self.retention_hours)

    def download_url(self, *, job_id: int, output_id: int) -> str:
        output = self.repository.get_downloadable_output(job_id=job_id, output_id=output_id)
        return self.storage.create_download_url(
            object_key=output.object_key,
            filename=output.filename,
        )

    def cleanup_expired(self, *, limit: int = 100) -> int:
        self.repository.expire_due_jobs(limit=limit)
        outputs = self.repository.list_cleanup_outputs(limit=limit)
        touched_jobs: set[int] = set()
        for output in outputs:
            if self.storage.object_exists(output.object_key):
                self.storage.delete_object(output.object_key)
            touched_jobs.add(self.repository.mark_output_deleted(output.id))
        for job_id in touched_jobs:
            self.repository.mark_job_deleted_if_empty(job_id)
        return len(outputs)


def _output_record(row: dict[str, object]) -> OutputRecord:
    expires = row["expires_at_utc"]
    if not isinstance(expires, datetime):
        raise TypeError("Expected PostgreSQL expires_at_utc to be a datetime")
    return OutputRecord(
        id=int(row["id"]),
        job_id=int(row["job_id"]),
        role=str(row["role"]),
        object_key=str(row["object_key"]),
        filename=str(row["filename"]),
        content_type=str(row["content_type"]) if row["content_type"] is not None else None,
        size_bytes=int(row["size_bytes"]),
        sha256=str(row["sha256"]),
        expires_at_utc=expires,
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(UTC)


def _validate_role(object_key: str, published: PublishedObject) -> str:
    parts = object_key.split("/")
    if len(parts) < 4 or parts[0] != "delivery":
        raise ValueError("Published object key is outside the delivery namespace")
    role = parts[2]
    if role not in {"source", "ableton", "wav24", "listen", "package"}:
        raise ValueError(f"Unsupported delivery role: {role}")
    if not object_key.endswith(published.sha256 + Path(object_key).suffix):
        raise ValueError("Published object key does not match the output digest")
    return role
