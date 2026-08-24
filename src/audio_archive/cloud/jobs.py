from __future__ import annotations

import json
from dataclasses import dataclass

from ..acquisition import AcquisitionResult
from ..resolver import ResolutionDecision
from .db import CloudDatabase
from .models import (
    ACTIVE_PROCESSING_STATES,
    DeliveryState,
    ProcessingState,
    WorkerNetworkClass,
    ensure_processing_transition,
)


@dataclass(frozen=True)
class RecoverySummary:
    finalized_publications: int
    requeued_jobs: int
    cleared_claims: int


class CloudJobRepository:
    def __init__(self, database: CloudDatabase):
        self.database = database

    def start_attempt(
        self,
        *,
        job_id: int,
        worker_id: str,
        network_class: WorkerNetworkClass,
    ) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO processing_attempts (job_id, worker_id, worker_network_class)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (job_id, worker_id, network_class.value),
            ).fetchone()
            assert row is not None
            return int(row["id"])

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        result: str,
        error_class: str | None = None,
        error_summary: str | None = None,
        tool_versions: dict[str, str | None] | None = None,
    ) -> None:
        with self.database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE processing_attempts
                SET ended_at_utc = NOW(), result = %s, error_class = %s,
                    error_summary = %s, tool_versions_json = %s::jsonb
                WHERE id = %s
                """,
                (
                    result,
                    error_class,
                    error_summary,
                    json.dumps(tool_versions or {}, sort_keys=True),
                    attempt_id,
                ),
            )
            if updated.rowcount != 1:
                raise KeyError(f"Processing attempt {attempt_id} does not exist")

    def record_resolution(self, job_id: int, decision: ResolutionDecision) -> ProcessingState:
        if decision.method == "automatic":
            target = ProcessingState.READY
        elif decision.method == "needs_review":
            target = ProcessingState.NEEDS_REVIEW
        else:
            target = ProcessingState.NOT_FOUND

        selected = (
            decision.selected.candidate
            if decision.method == "automatic" and decision.selected
            else None
        )
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT processing_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            old = ProcessingState(str(job["processing_state"]))
            if old is not ProcessingState.RESOLVING:
                raise ValueError(f"Job {job_id} must be resolving to record resolution")
            ensure_processing_transition(old, target)

            connection.execute("DELETE FROM candidates WHERE job_id = %s", (job_id,))
            for position, scored in enumerate(decision.ranked, start=1):
                candidate = scored.candidate
                connection.execute(
                    """
                    INSERT INTO candidates (
                        job_id, position, video_id, url, title, channel,
                        duration_seconds, thumbnail_url, score,
                        reasons_json, warnings_json, disqualified
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                    """,
                    (
                        job_id,
                        position,
                        candidate.video_id,
                        candidate.url or f"https://www.youtube.com/watch?v={candidate.video_id}",
                        candidate.title,
                        candidate.channel or None,
                        candidate.duration_seconds,
                        candidate.thumbnail_url,
                        scored.score,
                        json.dumps(scored.reasons, ensure_ascii=False),
                        json.dumps(scored.warnings, ensure_ascii=False),
                        scored.disqualified,
                    ),
                )

            connection.execute(
                """
                UPDATE jobs
                SET processing_state = %s,
                    source_extractor = %s,
                    source_id = %s,
                    source_url = %s,
                    source_title = %s,
                    source_creator = %s,
                    resolution_method = %s,
                    selected_score = %s,
                    runner_up_score = %s,
                    updated_at_utc = NOW(),
                    completed_at_utc = CASE WHEN %s = 'not_found' THEN NOW() ELSE completed_at_utc END
                WHERE id = %s
                """,
                (
                    target.value,
                    "youtube" if selected else None,
                    selected.video_id if selected else None,
                    selected.url if selected else None,
                    selected.title if selected else None,
                    selected.channel if selected and selected.channel else None,
                    decision.method,
                    decision.selected_score,
                    decision.runner_up_score,
                    target.value,
                    job_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_processing_state, to_processing_state,
                    event_type, message, detail_json
                ) VALUES (%s, 'resolving', %s, 'resolution', %s, %s::jsonb)
                """,
                (
                    job_id,
                    target.value,
                    f"Resolution finished as {decision.method}",
                    json.dumps(
                        {
                            "candidate_count": len(decision.ranked),
                            "selected_video_id": selected.video_id if selected else None,
                            "selected_score": decision.selected_score,
                            "runner_up_score": decision.runner_up_score,
                            "margin": decision.margin,
                        },
                        sort_keys=True,
                    ),
                ),
            )
        return target

    def record_acquisition(self, job_id: int, result: AcquisitionResult) -> None:
        warning_summary = "; ".join(warning.message for warning in result.warnings) or None
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT processing_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            if job["processing_state"] != ProcessingState.VERIFYING_MASTER.value:
                raise ValueError("Acquisition metadata may only be recorded after master verification")
            connection.execute(
                """
                UPDATE jobs
                SET source_title = %s,
                    source_creator = %s,
                    quality_status = %s,
                    warning_summary = %s,
                    updated_at_utc = NOW()
                WHERE id = %s
                """,
                (
                    result.source_title,
                    result.source_creator,
                    result.quality_status,
                    warning_summary,
                    job_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_processing_state, to_processing_state,
                    event_type, message, detail_json
                ) VALUES (
                    %s, 'verifying_master', 'verifying_master', 'source_verified',
                    'Source master passed verification', %s::jsonb
                )
                """,
                (
                    job_id,
                    json.dumps(
                        {
                            "sha256": result.master_sha256,
                            "quality_status": result.quality_status,
                            "audio_codec": result.probe.audio.codec,
                            "sample_rate_hz": result.probe.audio.sample_rate_hz,
                            "channels": result.probe.audio.channels,
                            "reused_existing": result.reused_existing,
                        },
                        sort_keys=True,
                    ),
                ),
            )

    def fail_job(
        self,
        job_id: int,
        *,
        stage: str,
        error_class: str,
        summary: str,
    ) -> None:
        concise = summary.strip()[-2000:] or error_class
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT processing_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            old = ProcessingState(str(job["processing_state"]))
            if old in {ProcessingState.COMPLETED, ProcessingState.COMPLETED_WITH_WARNINGS}:
                return
            if old is not ProcessingState.FAILED:
                ensure_processing_transition(old, ProcessingState.FAILED)
            connection.execute(
                """
                UPDATE jobs
                SET processing_state = 'failed', error_stage = %s, error_class = %s,
                    error_summary = %s, updated_at_utc = NOW()
                WHERE id = %s
                """,
                (stage, error_class, concise, job_id),
            )
            if old is not ProcessingState.FAILED:
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, from_processing_state, to_processing_state,
                        event_type, message
                    ) VALUES (%s, %s, 'failed', 'processing_failed', %s)
                    """,
                    (job_id, old.value, concise),
                )

    def recover_expired_claims(self) -> RecoverySummary:
        finalized = 0
        requeued = 0
        cleared = 0
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.id, j.processing_state, j.delivery_state, j.quality_status,
                       j.source_id, wc.worker_id
                FROM worker_claims AS wc
                JOIN jobs AS j ON j.id = wc.job_id
                WHERE wc.lease_expires_at_utc <= NOW()
                ORDER BY j.id
                FOR UPDATE OF j, wc
                """
            ).fetchall()
            for row in rows:
                job_id = int(row["id"])
                state = ProcessingState(str(row["processing_state"]))
                delivery = DeliveryState(str(row["delivery_state"]))
                if state is ProcessingState.PUBLISHING and delivery is DeliveryState.AVAILABLE:
                    final = (
                        ProcessingState.COMPLETED
                        if row["quality_status"] == "verified_best_available"
                        else ProcessingState.COMPLETED_WITH_WARNINGS
                    )
                    connection.execute(
                        """
                        UPDATE jobs
                        SET processing_state = %s, completed_at_utc = NOW(), updated_at_utc = NOW()
                        WHERE id = %s
                        """,
                        (final.value, job_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO job_events (
                            job_id, from_processing_state, to_processing_state,
                            event_type, message
                        ) VALUES (
                            %s, 'publishing', %s, 'worker_recovery',
                            'Recovered completed publication after worker lease expiry'
                        )
                        """,
                        (job_id, final.value),
                    )
                    finalized += 1
                elif state in ACTIVE_PROCESSING_STATES:
                    target = ProcessingState.READY if row["source_id"] else ProcessingState.PENDING
                    connection.execute(
                        """
                        UPDATE jobs
                        SET processing_state = %s, updated_at_utc = NOW(), retry_count = retry_count + 1
                        WHERE id = %s
                        """,
                        (target.value, job_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO job_events (
                            job_id, from_processing_state, to_processing_state,
                            event_type, message
                        ) VALUES (%s, %s, %s, 'worker_recovery', %s)
                        """,
                        (
                            job_id,
                            state.value,
                            target.value,
                            f"Recovered expired worker lease from {row['worker_id']}",
                        ),
                    )
                    requeued += 1
                connection.execute("DELETE FROM worker_claims WHERE job_id = %s", (job_id,))
                cleared += 1
        return RecoverySummary(finalized, requeued, cleared)

    def get_processing_attempt(self, attempt_id: int) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM processing_attempts WHERE id = %s",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Processing attempt {attempt_id} does not exist")
        return dict(row)
