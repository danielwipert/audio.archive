from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from ..urls import parse_youtube_url
from .db import CloudDatabase
from .models import DeliveryState, ProcessingState, ensure_processing_transition


@dataclass(frozen=True)
class JobView:
    job: dict[str, object]
    candidates: tuple[dict[str, object], ...]
    events: tuple[dict[str, object], ...]
    outputs: tuple[dict[str, object], ...]


class CloudWebRepository:
    def __init__(self, database: CloudDatabase):
        self.database = database

    def list_jobs(self, *, limit: int = 100) -> list[dict[str, object]]:
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM jobs
                ORDER BY created_at_utc DESC, id DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job_view(self, job_id: int) -> JobView:
        job = self.database.get_job(job_id)
        with self.database.connect() as connection:
            candidates = connection.execute(
                "SELECT * FROM candidates WHERE job_id = %s ORDER BY position",
                (job_id,),
            ).fetchall()
            events = connection.execute(
                """
                SELECT * FROM job_events
                WHERE job_id = %s
                ORDER BY occurred_at_utc DESC, id DESC
                LIMIT 100
                """,
                (job_id,),
            ).fetchall()
            outputs = connection.execute(
                """
                SELECT * FROM outputs
                WHERE job_id = %s
                ORDER BY role, id
                """,
                (job_id,),
            ).fetchall()
        return JobView(
            job=job,
            candidates=tuple(dict(row) for row in candidates),
            events=tuple(dict(row) for row in events),
            outputs=tuple(dict(row) for row in outputs),
        )

    def approve_candidate(self, job_id: int, video_id: str) -> None:
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT processing_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            old = ProcessingState(str(job["processing_state"]))
            if old is not ProcessingState.NEEDS_REVIEW:
                raise ValueError(f"Job {job_id} is not waiting for review")
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE job_id = %s AND video_id = %s",
                (job_id, video_id),
            ).fetchone()
            if candidate is None:
                raise ValueError(f"Candidate {video_id} is not recorded for job {job_id}")
            ensure_processing_transition(old, ProcessingState.READY)
            connection.execute(
                """
                UPDATE jobs
                SET processing_state = 'ready',
                    source_extractor = 'youtube',
                    source_id = %s,
                    source_url = %s,
                    source_title = %s,
                    source_creator = %s,
                    resolution_method = 'manual_selection',
                    selected_score = %s,
                    updated_at_utc = NOW(),
                    completed_at_utc = NULL
                WHERE id = %s
                """,
                (
                    candidate["video_id"],
                    candidate["url"],
                    candidate["title"],
                    candidate["channel"],
                    candidate["score"],
                    job_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_processing_state, to_processing_state,
                    event_type, message, detail_json
                ) VALUES (
                    %s, 'needs_review', 'ready', 'manual_resolution', %s, %s::jsonb
                )
                """,
                (
                    job_id,
                    f"User approved candidate {video_id}",
                    json.dumps(
                        {"video_id": video_id, "score": int(candidate["score"])},
                        sort_keys=True,
                    ),
                ),
            )

    def replace_source_url(self, job_id: int, url: str) -> None:
        pinned = parse_youtube_url(url)
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT processing_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            old = ProcessingState(str(job["processing_state"]))
            if old is not ProcessingState.NEEDS_REVIEW:
                raise ValueError(f"Job {job_id} is not waiting for review")
            ensure_processing_transition(old, ProcessingState.READY)
            connection.execute(
                """
                UPDATE jobs
                SET processing_state = 'ready',
                    source_extractor = 'youtube',
                    source_id = %s,
                    source_url = %s,
                    source_title = NULL,
                    source_creator = NULL,
                    resolution_method = 'manual_url',
                    selected_score = NULL,
                    runner_up_score = NULL,
                    updated_at_utc = NOW(),
                    completed_at_utc = NULL
                WHERE id = %s
                """,
                (pinned.video_id, pinned.canonical_url, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_processing_state, to_processing_state,
                    event_type, message, detail_json
                ) VALUES (
                    %s, 'needs_review', 'ready', 'manual_resolution', %s, %s::jsonb
                )
                """,
                (
                    job_id,
                    f"User supplied replacement source {pinned.video_id}",
                    json.dumps(
                        {"video_id": pinned.video_id, "url": pinned.canonical_url},
                        sort_keys=True,
                    ),
                ),
            )

    def mark_not_found(self, job_id: int) -> None:
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT processing_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            old = ProcessingState(str(job["processing_state"]))
            if old is not ProcessingState.NEEDS_REVIEW:
                raise ValueError(f"Job {job_id} is not waiting for review")
            ensure_processing_transition(old, ProcessingState.NOT_FOUND)
            connection.execute(
                """
                UPDATE jobs
                SET processing_state = 'not_found',
                    source_extractor = NULL,
                    source_id = NULL,
                    source_url = NULL,
                    source_title = NULL,
                    source_creator = NULL,
                    resolution_method = 'manual_not_found',
                    updated_at_utc = NOW(),
                    completed_at_utc = NOW()
                WHERE id = %s
                """,
                (job_id,),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_processing_state, to_processing_state,
                    event_type, message
                ) VALUES (
                    %s, 'needs_review', 'not_found', 'manual_resolution',
                    'User marked the request not found'
                )
                """,
                (job_id,),
            )

    def retry_job(self, job_id: int) -> ProcessingState:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT processing_state, delivery_state, source_extractor, source_id, source_url
                FROM jobs WHERE id = %s FOR UPDATE
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            old = ProcessingState(str(row["processing_state"]))
            delivery = DeliveryState(str(row["delivery_state"]))
            if old not in {ProcessingState.FAILED, ProcessingState.INTERRUPTED}:
                raise ValueError("Only failed or interrupted jobs can be retried")
            if delivery is not DeliveryState.NOT_PUBLISHED:
                raise ValueError("A job with published delivery cannot be retried")
            pinned = (
                row["source_extractor"] == "youtube"
                and row["source_id"] is not None
                and row["source_url"] is not None
            )
            target = ProcessingState.READY if pinned else ProcessingState.PENDING
            ensure_processing_transition(old, target)
            connection.execute(
                """
                UPDATE jobs
                SET processing_state = %s,
                    retry_count = retry_count + 1,
                    error_stage = NULL,
                    error_class = NULL,
                    error_summary = NULL,
                    updated_at_utc = NOW(),
                    started_at_utc = NULL,
                    completed_at_utc = NULL
                WHERE id = %s
                """,
                (target.value, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_processing_state, to_processing_state,
                    event_type, message
                ) VALUES (%s, %s, %s, 'retry_requested', 'User requested processing retry')
                """,
                (job_id, old.value, target.value),
            )
            return target

    def summarize_counts(self) -> dict[str, int]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT processing_state, COUNT(*) AS count FROM jobs GROUP BY processing_state"
            ).fetchall()
        counts = {str(row["processing_state"]): int(row["count"]) for row in rows}
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0) + counts.get("ready", 0),
            "active": sum(
                counts.get(state, 0)
                for state in (
                    "resolving",
                    "downloading",
                    "verifying_master",
                    "converting",
                    "verifying_output",
                    "packaging",
                    "publishing",
                )
            ),
            "review": counts.get("needs_review", 0),
            "failed": counts.get("failed", 0),
        }


def format_timestamp(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value) if value else None
