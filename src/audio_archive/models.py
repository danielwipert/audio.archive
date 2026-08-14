from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class JobState(StrEnum):
    PENDING = "pending"
    RESOLVING = "resolving"
    NEEDS_REVIEW = "needs_review"
    READY = "ready"
    DOWNLOADING = "downloading"
    VERIFYING_MASTER = "verifying_master"
    CONVERTING = "converting"
    VERIFYING_OUTPUT = "verifying_output"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    NOT_FOUND = "not_found"
    CANCELLED = "cancelled"


ACTIVE_STATES = {
    JobState.RESOLVING,
    JobState.DOWNLOADING,
    JobState.VERIFYING_MASTER,
    JobState.CONVERTING,
    JobState.VERIFYING_OUTPUT,
}

TERMINAL_STATES = {
    JobState.COMPLETED,
    JobState.COMPLETED_WITH_WARNINGS,
    JobState.SKIPPED_DUPLICATE,
    JobState.NOT_FOUND,
    JobState.CANCELLED,
}

ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.PENDING: {JobState.RESOLVING, JobState.READY, JobState.CANCELLED, JobState.FAILED},
    JobState.RESOLVING: {
        JobState.NEEDS_REVIEW,
        JobState.READY,
        JobState.NOT_FOUND,
        JobState.FAILED,
        JobState.INTERRUPTED,
    },
    JobState.NEEDS_REVIEW: {JobState.READY, JobState.NOT_FOUND, JobState.CANCELLED},
    JobState.READY: {
        JobState.DOWNLOADING,
        JobState.SKIPPED_DUPLICATE,
        JobState.CANCELLED,
        JobState.FAILED,
    },
    JobState.DOWNLOADING: {
        JobState.VERIFYING_MASTER,
        JobState.FAILED,
        JobState.INTERRUPTED,
    },
    JobState.VERIFYING_MASTER: {
        JobState.CONVERTING,
        JobState.COMPLETED,
        JobState.COMPLETED_WITH_WARNINGS,
        JobState.FAILED,
        JobState.INTERRUPTED,
    },
    JobState.CONVERTING: {
        JobState.VERIFYING_OUTPUT,
        JobState.FAILED,
        JobState.INTERRUPTED,
    },
    JobState.VERIFYING_OUTPUT: {
        JobState.COMPLETED,
        JobState.COMPLETED_WITH_WARNINGS,
        JobState.FAILED,
        JobState.INTERRUPTED,
    },
    JobState.FAILED: {JobState.PENDING, JobState.READY, JobState.CANCELLED},
    JobState.INTERRUPTED: {JobState.PENDING, JobState.READY, JobState.FAILED, JobState.CANCELLED},
    JobState.COMPLETED: set(),
    JobState.COMPLETED_WITH_WARNINGS: set(),
    JobState.SKIPPED_DUPLICATE: set(),
    JobState.NOT_FOUND: set(),
    JobState.CANCELLED: set(),
}


def ensure_transition(old: JobState, new: JobState) -> None:
    if new not in ALLOWED_TRANSITIONS[old]:
        raise ValueError(f"Invalid job transition: {old.value} -> {new.value}")


@dataclass(frozen=True)
class JobRequest:
    artist: str | None = None
    title: str | None = None
    version: str | None = None
    url: str | None = None
    profile: str = "ableton"
    origin: str = "manual"
    import_id: int | None = None
    import_row: int | None = None

    def validate(self) -> None:
        if not self.url and (not self.artist or not self.title):
            raise ValueError("artist and title are required unless a URL is supplied")
        if self.profile not in {"ableton", "archive", "listen", "complete"}:
            raise ValueError(f"Unknown output profile: {self.profile}")
        if self.origin not in {"manual", "url", "csv", "cli"}:
            raise ValueError(f"Unknown job origin: {self.origin}")

