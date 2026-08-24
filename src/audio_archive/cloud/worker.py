from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock, Thread

from .config import CloudSettings
from .db import CloudDatabase, LostWorkerClaim
from .execution import CloudExecutionRepository
from .models import ProcessingState, WorkerClaim
from .pipeline import CloudJobProcessor, CloudProcessingResult


class ClaimHeartbeat:
    """Renew a PostgreSQL worker lease while blocking media tools are running."""

    def __init__(
        self,
        database: CloudDatabase,
        claim: WorkerClaim,
        *,
        lease_seconds: int = 300,
        interval_seconds: float = 60.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if interval_seconds <= 0 or interval_seconds >= lease_seconds:
            raise ValueError("interval_seconds must be positive and shorter than the lease")
        self.database = database
        self.claim = claim
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._lock = Lock()
        self._error: BaseException | None = None
        self._thread: Thread | None = None

    def __enter__(self) -> "ClaimHeartbeat":
        self._thread = Thread(
            target=self._run,
            name=f"audio-archive-heartbeat-{self.claim.job_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        if exc is None:
            self.check()

    def check(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            if isinstance(error, LostWorkerClaim):
                raise error
            raise LostWorkerClaim(
                f"Heartbeat failed for job {self.claim.job_id}: {error}"
            ) from error

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.database.heartbeat_claim(
                    self.claim,
                    lease_seconds=self.lease_seconds,
                )
            except BaseException as exc:  # noqa: BLE001 - surfaced synchronously by check()
                with self._lock:
                    self._error = exc
                self._stop.set()
                return


@dataclass(frozen=True)
class CloudWorkerResult:
    job_id: int
    state: ProcessingState
    output_ids: tuple[int, ...] = ()
    error: str | None = None


class CloudSequentialWorker:
    """Claim and process one cloud job at a time with a renewable database lease."""

    def __init__(
        self,
        *,
        database: CloudDatabase,
        settings: CloudSettings,
        processor: CloudJobProcessor,
        lease_seconds: int = 300,
        heartbeat_interval_seconds: float = 60.0,
    ) -> None:
        self.database = database
        self.settings = settings
        self.processor = processor
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.execution = CloudExecutionRepository(database)

    def recover_startup(self) -> tuple[int, ...]:
        """Reconcile jobs abandoned after a worker process disappeared."""

        return self.execution.recover_abandoned_jobs()

    def run_next(self) -> CloudWorkerResult | None:
        claim = self.database.claim_next_job(
            worker_id=self.settings.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return None
        processing: CloudProcessingResult | None = None
        error: str | None = None
        lost_claim = False
        try:
            with ClaimHeartbeat(
                self.database,
                claim,
                lease_seconds=self.lease_seconds,
                interval_seconds=self.heartbeat_interval_seconds,
            ) as heartbeat:
                processing = self.processor.process_claim(claim, heartbeat=heartbeat)
        except LostWorkerClaim as exc:
            lost_claim = True
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - processor records durable job failure
            error = str(exc)
        finally:
            if not lost_claim:
                try:
                    self.database.release_claim(claim)
                except LostWorkerClaim:
                    lost_claim = True
                    if error is None:
                        error = f"Worker lost its claim for job {claim.job_id} during release"

        state = ProcessingState(str(self.database.get_job(claim.job_id)["processing_state"]))
        if processing is not None and error is None:
            return CloudWorkerResult(
                job_id=claim.job_id,
                state=processing.state,
                output_ids=processing.output_ids,
            )
        return CloudWorkerResult(
            job_id=claim.job_id,
            state=state,
            output_ids=processing.output_ids if processing else (),
            error=error,
        )

    def run_until_idle(self, *, max_jobs: int | None = None) -> tuple[CloudWorkerResult, ...]:
        if max_jobs is not None and max_jobs <= 0:
            raise ValueError("max_jobs must be positive when supplied")
        results: list[CloudWorkerResult] = []
        while max_jobs is None or len(results) < max_jobs:
            result = self.run_next()
            if result is None:
                break
            results.append(result)
        return tuple(results)
