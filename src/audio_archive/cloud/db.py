from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from ..urls import parse_youtube_url
from .models import (
    CloudJobRequest,
    DeliveryState,
    ProcessingState,
    WorkerClaim,
    ensure_delivery_transition,
    ensure_processing_transition,
)

DEFAULT_LEASE_SECONDS = 300


class LostWorkerClaim(RuntimeError):
    """Raised when a worker tries to use a missing, expired, or replaced claim."""


class CloudDatabase:
    def __init__(self, dsn: str):
        self.dsn = dsn

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            yield connection

    def apply_migrations(self, migrations_dir: Path) -> list[int]:
        """Apply pending numbered SQL migrations and return versions applied now."""
        applied_now: list[int] = []
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            applied = {
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            }
            for path in sorted(migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version in applied:
                    continue
                sql = path.read_text(encoding="utf-8")
                connection.execute(sql)
                applied_now.append(version)
                applied.add(version)
        return applied_now

    def create_job(self, request: CloudJobRequest) -> int:
        request.validate()
        processing_state = ProcessingState.READY if request.url else ProcessingState.PENDING
        pinned_source = parse_youtube_url(request.url) if request.url else None

        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO jobs (
                    processing_state, delivery_state, origin,
                    requested_artist, requested_title, requested_version, requested_url,
                    profile, import_id, import_row,
                    source_extractor, source_id, source_url, resolution_method
                ) VALUES (
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    processing_state.value,
                    DeliveryState.NOT_PUBLISHED.value,
                    request.origin,
                    request.artist,
                    request.title,
                    request.version,
                    request.url,
                    request.profile.value,
                    request.import_id,
                    request.import_row,
                    "youtube" if pinned_source else None,
                    pinned_source.video_id if pinned_source else None,
                    pinned_source.canonical_url if pinned_source else None,
                    "exact_url" if pinned_source else None,
                ),
            ).fetchone()
            assert row is not None
            job_id = int(row["id"])
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_processing_state, to_processing_state,
                    from_delivery_state, to_delivery_state,
                    event_type, message
                ) VALUES (%s, NULL, %s, NULL, %s, 'created', %s)
                """,
                (
                    job_id,
                    processing_state.value,
                    DeliveryState.NOT_PUBLISHED.value,
                    f"Created from {request.origin} input",
                ),
            )
            return job_id

    def get_job(self, job_id: int) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Job {job_id} does not exist")
        return dict(row)

    def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> WorkerClaim | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        with self.connect() as connection:
            while True:
                job = connection.execute(
                    """
                    SELECT j.id, j.processing_state
                    FROM jobs AS j
                    WHERE j.processing_state IN ('ready', 'pending')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM worker_claims AS active_claim
                          WHERE active_claim.job_id = j.id
                            AND active_claim.lease_expires_at_utc > NOW()
                      )
                    ORDER BY
                        CASE WHEN j.processing_state = 'ready' THEN 0 ELSE 1 END,
                        j.id
                    FOR UPDATE OF j SKIP LOCKED
                    LIMIT 1
                    """
                ).fetchone()
                if job is None:
                    return None

                job_id = int(job["id"])
                claim_token = uuid4()
                claim = connection.execute(
                    """
                    INSERT INTO worker_claims (
                        job_id, worker_id, claim_token,
                        claimed_at_utc, heartbeat_at_utc, lease_expires_at_utc
                    ) VALUES (
                        %s, %s, %s,
                        NOW(), NOW(), NOW() + make_interval(secs => %s)
                    )
                    ON CONFLICT (job_id) DO UPDATE
                    SET worker_id = EXCLUDED.worker_id,
                        claim_token = EXCLUDED.claim_token,
                        claimed_at_utc = NOW(),
                        heartbeat_at_utc = NOW(),
                        lease_expires_at_utc = EXCLUDED.lease_expires_at_utc
                    WHERE worker_claims.lease_expires_at_utc <= NOW()
                    RETURNING claimed_at_utc, lease_expires_at_utc
                    """,
                    (job_id, worker_id, claim_token, lease_seconds),
                ).fetchone()
                if claim is None:
                    # A previous worker renewed the lease after our SELECT.
                    # READ COMMITTED gives the next SELECT a fresh snapshot,
                    # so the renewed job is skipped and another job may be claimed.
                    continue

                return WorkerClaim(
                    job_id=job_id,
                    worker_id=worker_id,
                    claim_token=claim_token,
                    processing_state=ProcessingState(str(job["processing_state"])),
                    claimed_at_utc=_datetime(claim["claimed_at_utc"]),
                    lease_expires_at_utc=_datetime(claim["lease_expires_at_utc"]),
                )

    def heartbeat_claim(
        self,
        claim: WorkerClaim,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> WorkerClaim:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE worker_claims
                SET heartbeat_at_utc = NOW(),
                    lease_expires_at_utc = NOW() + make_interval(secs => %s)
                WHERE job_id = %s
                  AND worker_id = %s
                  AND claim_token = %s
                  AND lease_expires_at_utc > NOW()
                RETURNING claimed_at_utc, lease_expires_at_utc
                """,
                (lease_seconds, claim.job_id, claim.worker_id, claim.claim_token),
            ).fetchone()
            if row is None:
                raise LostWorkerClaim(f"Worker claim for job {claim.job_id} is no longer valid")
            return WorkerClaim(
                job_id=claim.job_id,
                worker_id=claim.worker_id,
                claim_token=claim.claim_token,
                processing_state=claim.processing_state,
                claimed_at_utc=_datetime(row["claimed_at_utc"]),
                lease_expires_at_utc=_datetime(row["lease_expires_at_utc"]),
            )

    def release_claim(self, claim: WorkerClaim) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """
                DELETE FROM worker_claims
                WHERE job_id = %s AND worker_id = %s AND claim_token = %s
                """,
                (claim.job_id, claim.worker_id, claim.claim_token),
            )
            if result.rowcount != 1:
                raise LostWorkerClaim(f"Worker claim for job {claim.job_id} is no longer valid")

    def transition_processing(
        self,
        job_id: int,
        new_state: ProcessingState,
        *,
        event_type: str = "state_transition",
        message: str | None = None,
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT processing_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            old_state = ProcessingState(str(row["processing_state"]))
            ensure_processing_transition(old_state, new_state)

            connection.execute(
                """
                UPDATE jobs
                SET processing_state = %s,
                    updated_at_utc = NOW(),
                    started_at_utc = CASE
                        WHEN started_at_utc IS NULL
                             AND %s IN (
                                'resolving', 'downloading', 'verifying_master',
                                'converting', 'verifying_output', 'packaging', 'publishing'
                             )
                        THEN NOW()
                        ELSE started_at_utc
                    END,
                    completed_at_utc = CASE
                        WHEN %s IN (
                            'completed', 'completed_with_warnings',
                            'skipped_duplicate', 'not_found', 'cancelled'
                        )
                        THEN NOW()
                        ELSE completed_at_utc
                    END
                WHERE id = %s
                """,
                (new_state.value, new_state.value, new_state.value, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_processing_state, to_processing_state,
                    event_type, message
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (job_id, old_state.value, new_state.value, event_type, message),
            )

    def transition_delivery(
        self,
        job_id: int,
        new_state: DeliveryState,
        *,
        event_type: str = "delivery_transition",
        message: str | None = None,
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT delivery_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            old_state = DeliveryState(str(row["delivery_state"]))
            ensure_delivery_transition(old_state, new_state)

            connection.execute(
                """
                UPDATE jobs
                SET delivery_state = %s,
                    updated_at_utc = NOW(),
                    deletion_requested_at_utc = CASE
                        WHEN %s = 'deletion_pending' THEN NOW()
                        ELSE deletion_requested_at_utc
                    END,
                    deleted_at_utc = CASE
                        WHEN %s = 'deleted' THEN NOW()
                        ELSE deleted_at_utc
                    END
                WHERE id = %s
                """,
                (new_state.value, new_state.value, new_state.value, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_delivery_state, to_delivery_state,
                    event_type, message
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (job_id, old_state.value, new_state.value, event_type, message),
            )


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"Expected datetime from PostgreSQL, got {type(value).__name__}")
    return value
