from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import uuid4

from .config import AppConfig
from .db import ArchiveDatabase
from .models import TERMINAL_STATES, JobState
from .pipeline import acquire_ready_job, create_ableton_for_job, create_listening_for_job
from .tooling import CommandRunner


@dataclass(frozen=True)
class RecoveryResult:
    interrupted_jobs: int
    stale_claims: int
    requeued_jobs: int


@dataclass(frozen=True)
class WorkerResult:
    job_id: int
    state: JobState
    error: str | None = None


class WorkerAlreadyRunning(ValueError):
    pass


def _claimed_process_is_alive(claim_token: str) -> bool:
    raw_pid, separator, _ = claim_token.partition(":")
    if not separator:
        return False
    try:
        process_id = int(raw_pid)
    except ValueError:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SequentialWorker:
    """Runs one durable archive job at a time and releases its claim on exit."""

    def __init__(
        self,
        database: ArchiveDatabase,
        config: AppConfig,
        runner: CommandRunner,
    ) -> None:
        self.database = database
        self.config = config
        self.runner = runner
        self._pause_requested = False

    def recover_startup(self) -> RecoveryResult:
        live_claims = [
            row
            for row in self.database.list_worker_claims()
            if _claimed_process_is_alive(str(row["claim_token"]))
        ]
        if live_claims:
            raise WorkerAlreadyRunning(
                f"Another queue worker is processing job {live_claims[0]['job_id']}"
            )
        interrupted = self.database.interrupt_active_jobs()
        stale_claims = self.database.clear_worker_claims()
        requeued = self.database.recover_interrupted_jobs()
        return RecoveryResult(interrupted, stale_claims, requeued)

    def request_pause(self) -> None:
        """Stop claiming work after the current job finishes."""

        self._pause_requested = True

    def resume(self) -> None:
        self._pause_requested = False

    @property
    def paused(self) -> bool:
        return self._pause_requested

    def run_next(self) -> WorkerResult | None:
        if self._pause_requested:
            return None
        claim_token = f"{os.getpid()}:{uuid4().hex}"
        job_id = self.database.claim_next_runnable_job(claim_token)
        if job_id is None:
            return None
        error: str | None = None
        try:
            self._execute_job(job_id)
        except Exception as exc:  # noqa: BLE001 - queue records failure and continues
            error = str(exc)
            current = JobState(self.database.get_job(job_id)["state"])
            if current not in TERMINAL_STATES | {JobState.FAILED}:
                try:
                    self.database.fail_job(job_id, stage=current.value, summary=error)
                except ValueError:
                    pass
        finally:
            if not self.database.release_worker_claim(job_id, claim_token):
                raise RuntimeError(f"Worker lost its claim for job {job_id}")
        return WorkerResult(job_id, JobState(self.database.get_job(job_id)["state"]), error)

    def run_until_idle(self) -> tuple[WorkerResult, ...]:
        results: list[WorkerResult] = []
        while not self._pause_requested:
            result = self.run_next()
            if result is None:
                break
            results.append(result)
        return tuple(results)

    def _execute_job(self, job_id: int) -> None:
        job = self.database.get_job(job_id)
        state = JobState(job["state"])
        if state == JobState.READY:
            acquire_ready_job(self.database, self.config, self.runner, job_id)
            job = self.database.get_job(job_id)
            state = JobState(job["state"])
        if state != JobState.CONVERTING:
            return

        profile = str(job["profile"])
        if profile == "ableton":
            create_ableton_for_job(self.database, self.config, self.runner, job_id)
        elif profile == "listen":
            create_listening_for_job(self.database, self.config, self.runner, job_id)
        elif profile == "complete":
            create_ableton_for_job(self.database, self.config, self.runner, job_id)
            if JobState(self.database.get_job(job_id)["state"]) == JobState.CONVERTING:
                create_listening_for_job(self.database, self.config, self.runner, job_id)
        else:
            raise ValueError(f"Profile {profile} unexpectedly entered conversion")
