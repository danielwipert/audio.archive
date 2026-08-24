from __future__ import annotations

import threading
from dataclasses import dataclass

from .db import CloudDatabase, LostWorkerClaim
from .jobs import CloudJobRepository, RecoverySummary
from .models import (
    ProcessingState,
    TERMINAL_PROCESSING_STATES,
    WorkerClaim,
    WorkerNetworkClass,
)
from .pipeline import CloudPipeline, CloudPipelineResult, classify_pipeline_failure


@dataclass(frozen=True)
class CloudWorkerResult:
    job_id: int
    state: ProcessingState
    error: str | None = None
    error_class: str | None = None


class LeaseKeeper:
    """Keep a PostgreSQL worker lease alive for the entire processing attempt."""

    def __init__(
        self,
        database: CloudDatabase,
        claim: WorkerClaim,
        *,
        lease_seconds: int = 300,
        heartbeat_seconds: float | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        interval = heartbeat_seconds if heartbeat_seconds is not None else min(60.0, lease_seconds / 3)
        if interval <= 0 or interval >= lease_seconds:
            raise ValueError("heartbeat interval must be positive and shorter than the lease")
        self.database = database
        self.claim = claim
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def __enter__(self) -> "LeaseKeeper":
        self.claim = self.database.heartbeat_claim(self.claim, lease_seconds=self.lease_seconds)
        self._thread = threading.Thread(
            target=self._run,
            name=f"audio-archive-lease-{self.claim.job_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if exc is None:
            self.check()

    def _run(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                self.claim = self.database.heartbeat_claim(
                    self.claim,
                    lease_seconds=self.lease_seconds,
                )
            except BaseException as exc:  # fail closed on any lease-refresh failure
                self._error = exc
                self._stop.set()
                return

    def check(self) -> None:
        if self._error is not None:
            if isinstance(self._error, LostWorkerClaim):
                raise self._error
            raise LostWorkerClaim(
                f"Could not refresh worker lease for job {self.claim.job_id}: {self._error}"
            ) from self._error


class CloudSequentialWorker:
    def __init__(
        self,
        *,
        database: CloudDatabase,
        pipeline: CloudPipeline,
        worker_id: str,
        network_class: WorkerNetworkClass = WorkerNetworkClass.UNKNOWN,
        lease_seconds: int = 300,
        heartbeat_seconds: float | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        self.database = database
        self.jobs = CloudJobRepository(database)
        self.pipeline = pipeline
        self.worker_id = worker_id
        self.network_class = network_class
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    def recover_startup(self) -> RecoverySummary:
        return self.jobs.recover_expired_claims()

    def run_next(self) -> CloudWorkerResult | None:
        claim = self.database.claim_next_job(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return None

        attempt_id = self.jobs.start_attempt(
            job_id=claim.job_id,
            worker_id=self.worker_id,
            network_class=self.network_class,
        )
        lost_claim = False
        try:
            with LeaseKeeper(
                self.database,
                claim,
                lease_seconds=self.lease_seconds,
                heartbeat_seconds=self.heartbeat_seconds,
            ) as keeper:
                result = self.pipeline.process(claim, ownership_check=keeper.check)
            self.jobs.finish_attempt(
                attempt_id,
                result=result.state.value,
                tool_versions=result.tool_versions or {},
            )
            return CloudWorkerResult(claim.job_id, result.state)
        except LostWorkerClaim as exc:
            lost_claim = True
            self.jobs.finish_attempt(
                attempt_id,
                result="interrupted",
                error_class="worker_lease_lost",
                error_summary=str(exc),
            )
            current = ProcessingState(str(self.database.get_job(claim.job_id)["processing_state"]))
            return CloudWorkerResult(
                claim.job_id,
                current,
                str(exc),
                "worker_lease_lost",
            )
        except Exception as exc:  # queue records failure and remains available for later retry
            job = self.database.get_job(claim.job_id)
            state = ProcessingState(str(job["processing_state"]))
            if state is ProcessingState.PUBLISHING and job["delivery_state"] == "available":
                final = (
                    ProcessingState.COMPLETED
                    if job["quality_status"] == "verified_best_available"
                    else ProcessingState.COMPLETED_WITH_WARNINGS
                )
                self.database.transition_processing(
                    claim.job_id,
                    final,
                    event_type="publication_recovery",
                    message="Recovered processing completion after delivery became available",
                )
                self.jobs.finish_attempt(
                    attempt_id,
                    result=final.value,
                    error_class="publication_finalize_recovered",
                    error_summary=str(exc),
                )
                return CloudWorkerResult(claim.job_id, final)

            error_class = classify_pipeline_failure(state, exc)
            if state not in TERMINAL_PROCESSING_STATES and state is not ProcessingState.NEEDS_REVIEW:
                self.jobs.fail_job(
                    claim.job_id,
                    stage=state.value,
                    error_class=error_class,
                    summary=str(exc),
                )
            self.jobs.finish_attempt(
                attempt_id,
                result="failed",
                error_class=error_class,
                error_summary=str(exc),
            )
            final_state = ProcessingState(
                str(self.database.get_job(claim.job_id)["processing_state"])
            )
            return CloudWorkerResult(claim.job_id, final_state, str(exc), error_class)
        finally:
            if not lost_claim:
                try:
                    self.database.release_claim(claim)
                except LostWorkerClaim:
                    pass

    def run_until_idle(self) -> tuple[CloudWorkerResult, ...]:
        results: list[CloudWorkerResult] = []
        while True:
            result = self.run_next()
            if result is None:
                return tuple(results)
            results.append(result)
