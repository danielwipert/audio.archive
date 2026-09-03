from __future__ import annotations

from pathlib import Path

import pytest

from audio_archive.tooling import CommandResult, ToolExecutionError

from audio_archive.cloud.config import CloudSettings
from audio_archive.cloud.execution import classify_job_error
from audio_archive.cloud.models import (
    AccessRetryPolicy,
    CloudJobRequest,
    CloudOutput,
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
    monkeypatch.delenv("AUDIO_ARCHIVE_SUBPROCESS_TIMEOUT_SECONDS", raising=False)

    settings = CloudSettings.from_env()

    assert settings.retention_hours == 24
    assert settings.signed_url_ttl_seconds == 900
    assert settings.subprocess_timeout_seconds == 1200
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


def test_subprocess_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        _settings(subprocess_timeout_seconds=0)


def _tool_failure(stderr: str) -> ToolExecutionError:
    return ToolExecutionError(
        CommandResult(
            argv=("yt-dlp", "https://youtu.be/example"),
            returncode=1,
            stdout="",
            stderr=stderr,
            started_at_utc="2026-09-02T23:16:20+00:00",
            finished_at_utc="2026-09-02T23:16:33+00:00",
        )
    )


def test_youtube_access_failure_is_classified_by_restriction() -> None:
    failure = _tool_failure("ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests")

    assert classify_job_error("downloading", failure) == "SourceAccessRateLimited"
    assert classify_job_error("resolving", failure) == "SourceAccessRateLimited"


def test_media_stage_failure_keeps_its_own_class() -> None:
    # A converting-stage tool failure is never reported as a YouTube access restriction,
    # even when its output mentions a status code.
    failure = _tool_failure("ERROR: HTTP Error 403 appears in this unrelated ffmpeg log line")

    assert classify_job_error("converting", failure) == "ToolExecutionError"


def test_non_tool_failures_keep_the_exception_name() -> None:
    assert classify_job_error("downloading", ValueError("bad manifest")) == "ValueError"


def test_access_retry_delay_grows_and_is_capped() -> None:
    policy = AccessRetryPolicy(limit=4, base_seconds=300, maximum_seconds=1200)

    assert [policy.delay_seconds(attempt) for attempt in (1, 2, 3, 4)] == [300, 600, 1200, 1200]


def test_access_retry_permits_only_transient_classes_within_the_limit() -> None:
    policy = AccessRetryPolicy(limit=2)

    assert policy.permits(error_class="SourceAccessRateLimited", attempts_used=0)
    assert policy.permits(error_class="SourceAccessBotCheck", attempts_used=1)
    # The budget is spent.
    assert not policy.permits(error_class="SourceAccessRateLimited", attempts_used=2)
    # A removed or private video does not become available by waiting.
    assert not policy.permits(error_class="SourceUnavailable", attempts_used=0)
    # A media fault is not an access failure.
    assert not policy.permits(error_class="ToolExecutionError", attempts_used=0)


def test_disabled_policy_never_permits_a_retry() -> None:
    assert not AccessRetryPolicy(limit=0).permits(
        error_class="SourceAccessRateLimited", attempts_used=0
    )


def test_expected_migration_versions_tracks_the_shipped_files() -> None:
    from audio_archive.cloud.runtime import expected_migration_versions

    root = Path(__file__).resolve().parents[1]
    versions = expected_migration_versions(root / "migrations")

    assert versions == {1, 2, 3}


def test_a_preset_still_decides_the_outputs_when_none_are_chosen() -> None:
    assert CloudJobRequest(url="https://youtu.be/x").resolved_outputs() == frozenset(
        {CloudOutput.ABLETON}
    )
    assert CloudJobRequest(
        url="https://youtu.be/x", profile=CloudProfile.SOURCE
    ).resolved_outputs() == frozenset()
    assert CloudJobRequest(
        url="https://youtu.be/x", profile=CloudProfile.PACKAGE
    ).resolved_outputs() == frozenset({CloudOutput.ABLETON, CloudOutput.PACKAGE})


def test_an_explicit_choice_wins_and_is_summarized_as_a_preset() -> None:
    chosen = frozenset({CloudOutput.WAV24, CloudOutput.LISTEN})
    request = CloudJobRequest(url="https://youtu.be/x", outputs=chosen)

    assert request.resolved_outputs() == chosen
    assert request.resolved_profile() is CloudProfile.ABLETON


def test_choosing_nothing_means_the_source_master_alone() -> None:
    request = CloudJobRequest(url="https://youtu.be/x", outputs=frozenset())

    assert request.resolved_outputs() == frozenset()
    assert request.resolved_profile() is CloudProfile.SOURCE


def test_a_package_request_is_summarized_as_the_package_preset() -> None:
    request = CloudJobRequest(
        url="https://youtu.be/x", outputs=frozenset({CloudOutput.PACKAGE})
    )

    assert request.resolved_profile() is CloudProfile.PACKAGE
