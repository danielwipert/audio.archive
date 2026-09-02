from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .models import WorkerNetworkClass


@dataclass(frozen=True, repr=False)
class CloudSettings:
    database_url: str
    r2_endpoint_url: str
    r2_bucket: str
    r2_access_key_id: str
    r2_secret_access_key: str
    scratch_root: Path
    worker_id: str
    worker_network_class: WorkerNetworkClass = WorkerNetworkClass.UNKNOWN
    retention_hours: int = 24
    signed_url_ttl_seconds: int = 900
    subprocess_timeout_seconds: int = 1200

    def __post_init__(self) -> None:
        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        if not self.r2_endpoint_url.startswith("https://"):
            raise ValueError("R2_ENDPOINT_URL must use HTTPS")
        if not self.r2_bucket.strip():
            raise ValueError("R2_BUCKET is required")
        if not self.r2_access_key_id.strip() or not self.r2_secret_access_key.strip():
            raise ValueError("R2 credentials are required")
        if not self.worker_id.strip():
            raise ValueError("AUDIO_ARCHIVE_WORKER_ID is required")
        if self.retention_hours <= 0:
            raise ValueError("AUDIO_ARCHIVE_RETENTION_HOURS must be positive")
        if not 60 <= self.signed_url_ttl_seconds <= 3600:
            raise ValueError("AUDIO_ARCHIVE_SIGNED_URL_TTL_SECONDS must be between 60 and 3600")
        if self.subprocess_timeout_seconds <= 0:
            raise ValueError("AUDIO_ARCHIVE_SUBPROCESS_TIMEOUT_SECONDS must be positive")

    def __repr__(self) -> str:
        return (
            "CloudSettings("
            f"database_url={_redact_url(self.database_url)!r}, "
            f"r2_endpoint_url={self.r2_endpoint_url!r}, "
            f"r2_bucket={self.r2_bucket!r}, "
            "r2_access_key_id='***', "
            "r2_secret_access_key='***', "
            f"scratch_root={self.scratch_root!r}, "
            f"worker_id={self.worker_id!r}, "
            f"worker_network_class={self.worker_network_class.value!r}, "
            f"retention_hours={self.retention_hours!r}, "
            f"signed_url_ttl_seconds={self.signed_url_ttl_seconds!r}, "
            f"subprocess_timeout_seconds={self.subprocess_timeout_seconds!r})"
        )

    @classmethod
    def from_env(cls) -> "CloudSettings":
        return cls(
            database_url=_required("DATABASE_URL"),
            r2_endpoint_url=_required("R2_ENDPOINT_URL"),
            r2_bucket=_required("R2_BUCKET"),
            r2_access_key_id=_required("R2_ACCESS_KEY_ID"),
            r2_secret_access_key=_required("R2_SECRET_ACCESS_KEY"),
            scratch_root=Path(os.getenv("AUDIO_ARCHIVE_SCRATCH_ROOT", "/work/jobs")),
            worker_id=_required("AUDIO_ARCHIVE_WORKER_ID"),
            worker_network_class=WorkerNetworkClass(
                os.getenv("AUDIO_ARCHIVE_WORKER_NETWORK_CLASS", "unknown")
            ),
            retention_hours=_positive_int("AUDIO_ARCHIVE_RETENTION_HOURS", 24),
            signed_url_ttl_seconds=_positive_int(
                "AUDIO_ARCHIVE_SIGNED_URL_TTL_SECONDS", 900
            ),
            subprocess_timeout_seconds=_positive_int(
                "AUDIO_ARCHIVE_SUBPROCESS_TIMEOUT_SECONDS", 1200
            ),
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _redact_url(value: str) -> str:
    if "@" not in value or "://" not in value:
        return "***"
    scheme, rest = value.split("://", 1)
    _, host = rest.rsplit("@", 1)
    return f"{scheme}://***@{host}"
