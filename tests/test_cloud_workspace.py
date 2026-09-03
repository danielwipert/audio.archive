from __future__ import annotations

import time
from pathlib import Path

import pytest

from audio_archive.cloud.config import CloudSettings
from audio_archive.cloud.models import WorkerNetworkClass
from audio_archive.cloud.workspace import CloudWorkspace, sweep_stale_workspaces
from audio_archive.integrity import write_sha256sums
from audio_archive.manifest import write_manifest_atomic

VIDEO_ID = "dQw4w9WgXcQ"


def _settings(scratch_root: Path) -> CloudSettings:
    return CloudSettings(
        database_url="postgresql://archive:secret@db.internal:5432/archive",
        r2_endpoint_url="https://example.r2.cloudflarestorage.com",
        r2_bucket="audio-archive-delivery",
        r2_access_key_id="access-key",
        r2_secret_access_key="super-secret",
        scratch_root=scratch_root,
        worker_id="worker-1",
        worker_network_class=WorkerNetworkClass.CLOUD_DATACENTER,
    )


def _acquired_item(workspace: CloudWorkspace, *, valid: bool = True) -> Path:
    item = workspace.archive_root / "items" / "youtube" / VIDEO_ID
    master_relative = Path("master") / f"{VIDEO_ID}.webm"
    master = item / master_relative
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"verified-native-source")
    write_manifest_atomic(
        item / "metadata" / "archive.json",
        {
            "schema_version": "1.2",
            "archive_id": f"youtube:{VIDEO_ID}",
            "content_type": "song",
            "request": {"origin": "url", "profile": "ableton"},
            "resolution": {},
            "source": {"platform": "youtube", "id": VIDEO_ID},
            "acquisition": {},
            "source_master": {"path": master_relative.as_posix()},
            "intermediates": [],
            "derivatives": [],
        },
    )
    write_sha256sums(item, [master_relative, Path("metadata/archive.json")])
    if not valid:
        master.write_bytes(b"tampered after the checksums were written")
    return item


def test_prepare_keeps_a_verified_item_and_clears_the_temporary_directory(
    tmp_path: Path,
) -> None:
    workspace = CloudWorkspace.for_job(_settings(tmp_path), 42)
    workspace.prepare()
    item = _acquired_item(workspace)
    partial = workspace.temp_directory / "42" / "source.webm.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"half a download")

    reusable = workspace.prepare()

    assert reusable == (VIDEO_ID,)
    assert item.is_dir()
    # A partial download carries no proof that it is complete, so it never survives.
    assert not partial.exists()
    assert workspace.temp_directory.is_dir()


def test_prepare_discards_an_item_that_fails_its_own_checksums(tmp_path: Path) -> None:
    workspace = CloudWorkspace.for_job(_settings(tmp_path), 42)
    workspace.prepare()
    item = _acquired_item(workspace, valid=False)

    assert workspace.prepare() == ()
    assert not item.exists()


def test_prepare_discards_an_item_with_no_manifest(tmp_path: Path) -> None:
    workspace = CloudWorkspace.for_job(_settings(tmp_path), 42)
    workspace.prepare()
    item = workspace.archive_root / "items" / "youtube" / VIDEO_ID
    (item / "master").mkdir(parents=True)
    (item / "master" / f"{VIDEO_ID}.webm").write_bytes(b"orphaned media")

    assert workspace.prepare() == ()
    assert not item.exists()


def test_discard_removes_the_whole_workspace(tmp_path: Path) -> None:
    workspace = CloudWorkspace.for_job(_settings(tmp_path), 42)
    workspace.prepare()
    _acquired_item(workspace)

    workspace.discard()

    assert not workspace.root.exists()


def test_a_workspace_cannot_escape_the_configured_scratch_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = CloudWorkspace(scratch_root=tmp_path, root=tmp_path)

    with pytest.raises(ValueError):
        workspace.discard()
    assert CloudWorkspace.for_job(settings, 42).root.parent == tmp_path.resolve()


def test_the_sweep_keeps_workspaces_a_later_attempt_could_still_use(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    for job_id in (1, 2, 3):
        CloudWorkspace.for_job(settings, job_id).prepare()
    (tmp_path / "not-a-job").mkdir()

    removed = sweep_stale_workspaces(
        settings,
        is_retainable=lambda job_id: job_id == 2,
        retention_hours=6,
    )

    assert removed == (1, 3)
    assert (tmp_path / "job-2").is_dir()
    assert (tmp_path / "not-a-job").is_dir()


def test_the_sweep_reclaims_a_retainable_workspace_once_it_goes_stale(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    workspace = CloudWorkspace.for_job(settings, 7)
    workspace.prepare()

    fresh = sweep_stale_workspaces(
        settings, is_retainable=lambda _: True, retention_hours=6
    )
    assert fresh == ()

    stale = sweep_stale_workspaces(
        settings,
        is_retainable=lambda _: True,
        retention_hours=6,
        now=time.time() + 7 * 3600,
    )

    assert stale == (7,)
    assert not workspace.root.exists()
