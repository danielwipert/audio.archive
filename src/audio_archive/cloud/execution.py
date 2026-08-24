from __future__ import annotations

import json
from dataclasses import dataclass

from ..resolver import CandidateScore, ResolutionDecision
from .db import CloudDatabase
from .models import (
    ACTIVE_PROCESSING_STATES,
    ALLOWED_PROCESSING_TRANSITIONS,
    DeliveryState,
    ProcessingState,
    WorkerNetworkClass,
    ensure_processing_transition,
)


@dataclass(frozen=True)
class ResolutionPersistence:
    state: ProcessingState
    selected_video_id: str | None


class CloudExecutionRepository:
    """Persistence used by the cloud worker around the reusable audio services."""

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
                INSERT INTO processing_attempts (
                    job_id, worker_id, worker_network_class
                ) VALUES (%s, %s, %s)
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
    ) -> None:
        with self.database.connect() as connection:
            update = connection.execute(
                """
                UPDATE processing_attempts
                SET ended_at_utc = NOW(),
                    result = %s,
                    error_class = %s,
                    error_summary = %s
                WHERE id = %s AND ended_at_utc IS NULL
                """,
                (result, error_class, error_summary, attempt_id),
            )
            if update.rowcount != 1:
                raise ValueError(f"Processing attempt {attempt_id} is already closed or missing")

    def recover_abandoned_jobs(self, *, limit: int = 100) -> tuple[int, ...]:
        """Requeue active jobs whose worker lease is no longer valid.

        A publishing job that already made delivery available is finalized rather than
        reprocessed because its verified outputs have already crossed the publication
        boundary. Other abandoned jobs are marked interrupted and requeued from their
        pinned source, or from pending resolution when no source is pinned.
        """

        if limit <= 0:
            raise ValueError("limit must be positive")
        active_values = tuple(state.value for state in ACTIVE_PROCESSING_STATES)
        recovered: list[int] = []
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.*
                FROM jobs AS j
                WHERE j.processing_state = ANY(%s)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM worker_claims AS c
                      WHERE c.job_id = j.id
                        AND c.lease_expires_at_utc > NOW()
                  )
                ORDER BY j.id
                FOR UPDATE OF j SKIP LOCKED
                LIMIT %s
                """,
                (list(active_values), limit),
            ).fetchall()
            for row in rows:
                job_id = int(row["id"])
                old_state = ProcessingState(str(row["processing_state"]))
                delivery_state = DeliveryState(str(row["delivery_state"]))

                connection.execute(
                    "DELETE FROM worker_claims WHERE job_id = %s AND lease_expires_at_utc <= NOW()",
                    (job_id,),
                )
                connection.execute(
                    """
                    UPDATE processing_attempts
                    SET ended_at_utc = COALESCE(ended_at_utc, NOW()),
                        result = COALESCE(result, 'interrupted'),
                        error_class = COALESCE(error_class, 'WorkerLeaseExpired'),
                        error_summary = COALESCE(
                            error_summary,
                            'Worker lease expired before the processing attempt completed'
                        )
                    WHERE job_id = %s AND ended_at_utc IS NULL
                    """,
                    (job_id,),
                )

                if (
                    old_state is ProcessingState.PUBLISHING
                    and delivery_state is DeliveryState.AVAILABLE
                ):
                    final_state = (
                        ProcessingState.COMPLETED
                        if str(row.get("quality_status") or "") == "verified_best_available"
                        else ProcessingState.COMPLETED_WITH_WARNINGS
                    )
                    ensure_processing_transition(old_state, final_state)
                    connection.execute(
                        """
                        UPDATE jobs
                        SET processing_state = %s,
                            completed_at_utc = COALESCE(completed_at_utc, NOW()),
                            updated_at_utc = NOW()
                        WHERE id = %s
                        """,
                        (final_state.value, job_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO job_events (
                            job_id, from_processing_state, to_processing_state,
                            event_type, message
                        ) VALUES (%s, %s, %s, 'recovered_after_publication', %s)
                        """,
                        (
                            job_id,
                            old_state.value,
                            final_state.value,
                            "Recovered successful publication after worker lease expiry",
                        ),
                    )
                    recovered.append(job_id)
                    continue

                ensure_processing_transition(old_state, ProcessingState.INTERRUPTED)
                target = (
                    ProcessingState.READY
                    if row.get("source_extractor") == "youtube"
                    and row.get("source_id")
                    and row.get("source_url")
                    else ProcessingState.PENDING
                )
                ensure_processing_transition(ProcessingState.INTERRUPTED, target)
                connection.execute(
                    """
                    UPDATE jobs
                    SET processing_state = %s,
                        updated_at_utc = NOW(),
                        error_stage = NULL,
                        error_class = NULL,
                        error_summary = NULL
                    WHERE id = %s
                    """,
                    (target.value, job_id),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, from_processing_state, to_processing_state,
                        event_type, message
                    ) VALUES (%s, %s, 'interrupted', 'worker_interrupted', %s)
                    """,
                    (
                        job_id,
                        old_state.value,
                        "Worker lease expired; ephemeral processing attempt was abandoned",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, from_processing_state, to_processing_state,
                        event_type, message
                    ) VALUES (%s, 'interrupted', %s, 'requeued_after_interruption', %s)
                    """,
                    (
                        job_id,
                        target.value,
                        "Abandoned job requeued from its durable source decision",
                    ),
                )
                recovered.append(job_id)
        return tuple(recovered)

    def persist_resolution(
        self,
        *,
        job_id: int,
        decision: ResolutionDecision,
    ) -> ResolutionPersistence:
        if decision.method == "automatic":
            target = ProcessingState.READY
        elif decision.method == "needs_review":
            target = ProcessingState.NEEDS_REVIEW
        else:
            target = ProcessingState.NOT_FOUND
        selected = decision.selected.candidate if decision.selected else None

        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT processing_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            old_state = ProcessingState(str(job["processing_state"]))
            if old_state is not ProcessingState.RESOLVING:
                raise ValueError(f"Job {job_id} must be resolving to record resolution")
            ensure_processing_transition(old_state, target)

            connection.execute("DELETE FROM candidates WHERE job_id = %s", (job_id,))
            self._insert_candidates(connection, job_id, decision.ranked)
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
                    completed_at_utc = CASE
                        WHEN %s = 'not_found' THEN NOW()
                        ELSE completed_at_utc
                    END
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
        return ResolutionPersistence(
            state=target,
            selected_video_id=selected.video_id if selected else None,
        )

    def record_acquisition(
        self,
        *,
        job_id: int,
        source_title: str,
        source_creator: str | None,
        quality_status: str,
        warnings: tuple[str, ...],
    ) -> None:
        warning_summary = "; ".join(warnings) if warnings else None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT processing_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            if str(row["processing_state"]) != ProcessingState.VERIFYING_MASTER.value:
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
                (source_title, source_creator, quality_status, warning_summary, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, event_type, message, detail_json
                ) VALUES (%s, 'master_verified', %s, %s::jsonb)
                """,
                (
                    job_id,
                    "Native source master passed verification",
                    json.dumps(
                        {
                            "quality_status": quality_status,
                            "warnings": list(warnings),
                        },
                        sort_keys=True,
                    ),
                ),
            )

    def fail_job(
        self,
        *,
        job_id: int,
        stage: str,
        error: BaseException,
    ) -> bool:
        """Fail a job if its current state still permits a transition to failed."""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT processing_state FROM jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            old_state = ProcessingState(str(row["processing_state"]))
            if ProcessingState.FAILED not in ALLOWED_PROCESSING_TRANSITIONS[old_state]:
                return False
            summary = str(error)[:4000]
            error_class = type(error).__name__
            connection.execute(
                """
                UPDATE jobs
                SET processing_state = 'failed',
                    error_stage = %s,
                    error_class = %s,
                    error_summary = %s,
                    updated_at_utc = NOW()
                WHERE id = %s
                """,
                (stage, error_class, summary, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, from_processing_state, to_processing_state,
                    event_type, message, detail_json
                ) VALUES (%s, %s, 'failed', 'failed', %s, %s::jsonb)
                """,
                (
                    job_id,
                    old_state.value,
                    summary,
                    json.dumps({"stage": stage, "error_class": error_class}, sort_keys=True),
                ),
            )
            return True

    @staticmethod
    def _insert_candidates(connection, job_id: int, ranked: tuple[CandidateScore, ...]) -> None:
        for position, scored in enumerate(ranked, start=1):
            candidate = scored.candidate
            connection.execute(
                """
                INSERT INTO candidates (
                    job_id, position, video_id, url, title, channel,
                    duration_seconds, thumbnail_url, score, reasons_json,
                    warnings_json, disqualified
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
