from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ProcessingState(StrEnum):
    PENDING = "pending"
    RESOLVING = "resolving"
    NEEDS_REVIEW = "needs_review"
    READY = "ready"
    DOWNLOADING = "downloading"
    VERIFYING_MASTER = "verifying_master"
    CONVERTING = "converting"
    VERIFYING_OUTPUT = "verifying_output"
    PACKAGING = "packaging"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    NOT_FOUND = "not_found"
    CANCELLED = "cancelled"


class DeliveryState(StrEnum):
    NOT_PUBLISHED = "not_published"
    AVAILABLE = "available"
    DELETION_PENDING = "deletion_pending"
    EXPIRED = "expired"
    DELETED = "deleted"


class CloudProfile(StrEnum):
    ABLETON = "ableton"
    SOURCE = "source"
    PACKAGE = "package"


class WorkerNetworkClass(StrEnum):
    CLOUD_DATACENTER = "cloud_datacenter"
    RESIDENTIAL = "residential"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CloudJobRequest:
    artist: str | None = None
    title: str | None = None
    version: str | None = None
    url: str | None = None
    profile: CloudProfile = CloudProfile.ABLETON
    origin: str = "manual"
    import_id: int | None = None
    import_row: int | None = None

    def validate(self) -> None:
        if not self.url and (not self.artist or not self.title):
            raise ValueError("artist and title are required unless a URL is supplied")
        if self.origin not in {"manual", "url", "csv", "cli"}:
            raise ValueError(f"Unknown job origin: {self.origin}")
        if self.import_row is not None and self.import_row <= 0:
            raise ValueError("import_row must be positive")


RETRYABLE_ACCESS_ERROR_CLASSES = frozenset(
    {
        "SourceAccessRateLimited",
        "SourceAccessBotCheck",
        "SourceAccessForbidden",
        "SourceAccessTokenFailure",
    }
)
"""Access failures that a later attempt from the same worker can plausibly clear.

`SourceUnavailable` is deliberately absent: a removed, private or region-blocked
video does not become available by waiting, so it stays a user decision.
"""


@dataclass(frozen=True)
class AccessRetryPolicy:
    """How often, and how far apart, the worker retries a source-access failure."""

    limit: int = 3
    base_seconds: int = 300
    maximum_seconds: int = 3600

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("limit cannot be negative")
        if self.base_seconds <= 0:
            raise ValueError("base_seconds must be positive")
        if self.maximum_seconds < self.base_seconds:
            raise ValueError("maximum_seconds cannot be shorter than base_seconds")

    def delay_seconds(self, attempt: int) -> int:
        """Return the wait before a 1-based attempt: 5, 10, 20 minutes by default."""

        if attempt < 1:
            raise ValueError("attempt must be positive")
        return min(self.base_seconds * 2 ** (attempt - 1), self.maximum_seconds)

    def permits(self, *, error_class: str, attempts_used: int) -> bool:
        return (
            error_class in RETRYABLE_ACCESS_ERROR_CLASSES
            and 0 <= attempts_used < self.limit
        )


@dataclass(frozen=True)
class WorkerClaim:
    job_id: int
    worker_id: str
    claim_token: UUID
    processing_state: ProcessingState
    claimed_at_utc: datetime
    lease_expires_at_utc: datetime


ACTIVE_PROCESSING_STATES = {
    ProcessingState.RESOLVING,
    ProcessingState.DOWNLOADING,
    ProcessingState.VERIFYING_MASTER,
    ProcessingState.CONVERTING,
    ProcessingState.VERIFYING_OUTPUT,
    ProcessingState.PACKAGING,
    ProcessingState.PUBLISHING,
}

SUCCESS_PROCESSING_STATES = {
    ProcessingState.COMPLETED,
    ProcessingState.COMPLETED_WITH_WARNINGS,
    ProcessingState.SKIPPED_DUPLICATE,
}

TERMINAL_PROCESSING_STATES = SUCCESS_PROCESSING_STATES | {
    ProcessingState.NOT_FOUND,
    ProcessingState.CANCELLED,
}


ALLOWED_PROCESSING_TRANSITIONS: dict[ProcessingState, set[ProcessingState]] = {
    ProcessingState.PENDING: {
        ProcessingState.RESOLVING,
        ProcessingState.READY,
        ProcessingState.CANCELLED,
        ProcessingState.FAILED,
    },
    ProcessingState.RESOLVING: {
        ProcessingState.NEEDS_REVIEW,
        ProcessingState.READY,
        ProcessingState.NOT_FOUND,
        ProcessingState.FAILED,
        ProcessingState.INTERRUPTED,
    },
    ProcessingState.NEEDS_REVIEW: {
        ProcessingState.READY,
        ProcessingState.NOT_FOUND,
        ProcessingState.CANCELLED,
    },
    ProcessingState.READY: {
        ProcessingState.DOWNLOADING,
        ProcessingState.SKIPPED_DUPLICATE,
        ProcessingState.CANCELLED,
        ProcessingState.FAILED,
    },
    ProcessingState.DOWNLOADING: {
        ProcessingState.VERIFYING_MASTER,
        ProcessingState.FAILED,
        ProcessingState.INTERRUPTED,
    },
    ProcessingState.VERIFYING_MASTER: {
        ProcessingState.CONVERTING,
        ProcessingState.PACKAGING,
        ProcessingState.PUBLISHING,
        ProcessingState.FAILED,
        ProcessingState.INTERRUPTED,
    },
    ProcessingState.CONVERTING: {
        ProcessingState.VERIFYING_OUTPUT,
        ProcessingState.FAILED,
        ProcessingState.INTERRUPTED,
    },
    ProcessingState.VERIFYING_OUTPUT: {
        ProcessingState.PACKAGING,
        ProcessingState.PUBLISHING,
        ProcessingState.FAILED,
        ProcessingState.INTERRUPTED,
    },
    ProcessingState.PACKAGING: {
        ProcessingState.PUBLISHING,
        ProcessingState.FAILED,
        ProcessingState.INTERRUPTED,
    },
    ProcessingState.PUBLISHING: {
        ProcessingState.COMPLETED,
        ProcessingState.COMPLETED_WITH_WARNINGS,
        ProcessingState.FAILED,
        ProcessingState.INTERRUPTED,
    },
    ProcessingState.FAILED: {
        ProcessingState.PENDING,
        ProcessingState.READY,
        ProcessingState.CANCELLED,
    },
    ProcessingState.INTERRUPTED: {
        ProcessingState.PENDING,
        ProcessingState.READY,
        ProcessingState.FAILED,
        ProcessingState.CANCELLED,
    },
    ProcessingState.COMPLETED: set(),
    ProcessingState.COMPLETED_WITH_WARNINGS: set(),
    ProcessingState.SKIPPED_DUPLICATE: set(),
    ProcessingState.NOT_FOUND: set(),
    ProcessingState.CANCELLED: set(),
}


ALLOWED_DELIVERY_TRANSITIONS: dict[DeliveryState, set[DeliveryState]] = {
    DeliveryState.NOT_PUBLISHED: {DeliveryState.AVAILABLE},
    DeliveryState.AVAILABLE: {
        DeliveryState.DELETION_PENDING,
        DeliveryState.EXPIRED,
        DeliveryState.DELETED,
    },
    DeliveryState.DELETION_PENDING: {
        DeliveryState.EXPIRED,
        DeliveryState.DELETED,
    },
    DeliveryState.EXPIRED: {DeliveryState.DELETED},
    DeliveryState.DELETED: set(),
}


def ensure_processing_transition(old: ProcessingState, new: ProcessingState) -> None:
    if new not in ALLOWED_PROCESSING_TRANSITIONS[old]:
        raise ValueError(f"Invalid processing transition: {old.value} -> {new.value}")


def ensure_delivery_transition(old: DeliveryState, new: DeliveryState) -> None:
    if new not in ALLOWED_DELIVERY_TRANSITIONS[old]:
        raise ValueError(f"Invalid delivery transition: {old.value} -> {new.value}")


def display_status(processing: ProcessingState, delivery: DeliveryState) -> str:
    """Return the user-facing cloud status without mutating processing history."""
    if (
        delivery in {DeliveryState.EXPIRED, DeliveryState.DELETED}
        and processing in SUCCESS_PROCESSING_STATES
    ):
        return "files_expired"
    if delivery is DeliveryState.AVAILABLE and processing in SUCCESS_PROCESSING_STATES:
        return "ready_to_download"
    return processing.value
