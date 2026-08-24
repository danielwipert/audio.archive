from __future__ import annotations

from pathlib import Path

import pytest

from audio_archive.cloud.config import CloudSettings
from audio_archive.cloud.models import (
    CloudProfile,
    DeliveryState,
    ProcessingState,
    WorkerNetworkClass,
    display_status,
    ensure_delivery_transition,
    ensure_processing_transition,
)


def _settings(**overrides: object) -> CloudSettings:
    values: dict[str, object] = {
        "database_url": "postgresql://archive:secret@db.internal:5432/archive",
        "r2_endpoint_url": "https://example.r2.cloudflarestorage.com",
        "r2_bucket": "audio-archive-delivery",
        "r2_access_key_id": "access-key",
        "r2_secret_access_key": "super-secret",
        "scratch_root": Path("/work/jobs"),
        "worker_id": "railway-worker-01",
        "worker_network_class": WorkerNetworkClass.CLOUD_DATACENTER,
        "retention_hours": 24,
        "signed_url_ttl_seconds": 900,
    }
    values.update(overrides)
    return CloudSettings(**values)  # type: ignore[arg-type]


def test_cloud_profiles_are_locked_to_v01_values() -> None:
    assert {profile.value for profile in CloudProfile} == {"ableton", "source", "package"}


def test_ready_to_download_is_derived_from_processing_and_delivery_state() -> None:
    assert (
        display_status(ProcessingState.COMPLETED, DeliveryState.AVAILABLE)
        == "ready_to_download"
    )
    assert (
        display_status(ProcessingState.COMPLETED_WITH_WARNINGS, DeliveryState.AVAILABLE)
        == "ready_to_download"
    )


def test_expiry_does_not_replace_processing_result() -> None:
    assert display_status(ProcessingState.COMPLETED, DeliveryState.EXPIRED) == "files_expired"
    assert display_status(ProcessingState.COMPLETED, DeliveryState.DELETED) == "files_expired"


def test_invalid_processing_transition_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid processing transition"):
        ensure_processing_transition(ProcessingState.PENDING, ProcessingState.COMPLETED)


def test_valid_processing_publication_path() -> None:
    ensure_processing_transition(ProcessingState.VERIFYING_OUTPUT, ProcessingState.PUBLISHING)
    ensure_processing_transition(ProcessingState.PUBLISHING, ProcessingState.COMPLETED)


def test_delivery_lifecycle_is_forward_only() -> None:
    ensure_delivery_transition(DeliveryState.NOT_PUBLISHED, DeliveryState.AVAILABLE)
    ensure_delivery_transition(DeliveryState.AVAILABLE, DeliveryState.EXPIRED)
    ensure_delivery_transition(DeliveryState.EXPIRED, DeliveryState.DELETED)
    with pytest.raises(ValueError, match="Invalid delivery transition"):
        ensure_delivery_transition(DeliveryState.DELETED, DeliveryState.AVAILABLE)


def test_cloud_settings_defaults_are_spec_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "DATABASE_URL": "postgresql://archive:secret@db.internal:5432/archive",
        "R2_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
        "R2_BUCKET": "audio-archive-delivery",
        "R2_ACCESS_KEY_ID": "access-key",
        "R2_SECRET_ACCESS_KEY": "super-secret",
        "AUDIO_ARCHIVE_WORKER_ID": "worker-01",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AUDIO_ARCHIVE_RETENTION_HOURS", raising=False)
    monkeypatch.delenv("AUDIO_ARCHIVE_SIGNED_URL_TTL_SECONDS", raising=False)

    settings = CloudSettings.from_env()

    assert settings.retention_hours == 24
    assert settings.signed_url_ttl_seconds == 900
    assert settings.worker_network_class is WorkerNetworkClass.UNKNOWN
    assert settings.scratch_root == Path("/work/jobs")


def test_cloud_settings_repr_redacts_credentials() -> None:
    rendered = repr(_settings())

    assert "super-secret" not in rendered
    assert "access-key" not in rendered
    assert "archive:secret" not in rendered
    assert "postgresql://***@db.internal:5432/archive" in rendered


def test_cloud_settings_require_https_r2_endpoint() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        _settings(r2_endpoint_url="http://example.invalid")


def test_signed_url_ttl_is_bounded() -> None:
    with pytest.raises(ValueError, match="between 60 and 3600"):
        _settings(signed_url_ttl_seconds=7200)
