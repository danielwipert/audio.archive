from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..config import AppConfig
from ..integrity import verify_sha256sums
from .config import CloudSettings
from .models import WorkerClaim

LOGGER = logging.getLogger("audio_archive.cloud.workspace")


@dataclass(frozen=True)
class CloudWorkspace:
    """Per-job scratch space that survives a failed attempt so the next one can reuse it.

    The workspace is keyed on the job rather than on the claim, because a job is only
    ever processed by the worker holding its lease. That lets a retry pick up the
    verified archive item an earlier attempt already published inside this workspace,
    which is what CLOUD_SPEC section 18.1 asks for: restart a stage only as far back as
    necessary, and never trust a temporary file merely because it exists.
    """

    scratch_root: Path
    root: Path

    @classmethod
    def for_job(cls, settings: CloudSettings, job_id: int) -> "CloudWorkspace":
        scratch_root = settings.scratch_root.expanduser().resolve()
        root = (scratch_root / f"job-{job_id}").resolve()
        if not root.is_relative_to(scratch_root) or root == scratch_root:
            raise ValueError("Cloud workspace escaped the configured scratch root")
        return cls(scratch_root=scratch_root, root=root)

    @classmethod
    def for_claim(cls, settings: CloudSettings, claim: WorkerClaim) -> "CloudWorkspace":
        return cls.for_job(settings, claim.job_id)

    @property
    def archive_root(self) -> Path:
        return self.root / "archive"

    @property
    def temp_directory(self) -> Path:
        return self.root / "temp"

    def prepare(self) -> tuple[str, ...]:
        """Keep what an earlier attempt proved, discard everything else.

        A published archive item carries its own manifest and checksums, so it can be
        re-verified independently and reused. Job temporary files carry no such proof:
        a partial download or a half-written derivative is indistinguishable from a
        complete one by inspection, so the temporary directory always starts empty.

        Returns the archive items that survived, for the caller to log.
        """

        self._assert_safe()
        self.archive_root.mkdir(parents=True, exist_ok=True)
        reusable = self._prune_unverified_items()
        if self.temp_directory.exists():
            shutil.rmtree(self.temp_directory)
        self.temp_directory.mkdir(parents=True, exist_ok=True)
        return reusable

    def local_config(self, base: AppConfig) -> AppConfig:
        """Reuse the proven local services against this ephemeral archive root."""

        return replace(
            base,
            archive_root=self.archive_root,
            temp_directory=self.temp_directory,
            database_path=self.root / "unused-local-state.db",
            host="127.0.0.1",
            open_browser=False,
        )

    def discard(self) -> None:
        """Remove the workspace entirely, once its outputs are published or abandoned."""

        self._assert_safe()
        shutil.rmtree(self.root, ignore_errors=True)

    def _item_directories(self) -> list[Path]:
        extractor_root = self.archive_root / "items"
        if not extractor_root.is_dir():
            return []
        return sorted(
            item
            for extractor in extractor_root.iterdir()
            if extractor.is_dir()
            for item in extractor.iterdir()
            if item.is_dir()
        )

    def _prune_unverified_items(self) -> tuple[str, ...]:
        reusable: list[str] = []
        for item in self._item_directories():
            manifest = item / "metadata" / "archive.json"
            if manifest.is_file() and verify_sha256sums(item).valid:
                reusable.append(item.name)
                continue
            LOGGER.info("Discarding unverifiable archive item in scratch: %s", item)
            shutil.rmtree(item, ignore_errors=True)
        return tuple(reusable)

    def _assert_safe(self) -> None:
        root = self.root.resolve()
        scratch = self.scratch_root.resolve()
        if root == scratch or not root.is_relative_to(scratch):
            raise ValueError("Refusing to modify an unsafe cloud workspace path")


def sweep_stale_workspaces(
    settings: CloudSettings,
    *,
    is_retainable: Callable[[int], bool],
    retention_hours: int,
    now: float | None = None,
) -> tuple[int, ...]:
    """Delete job workspaces that no attempt will use again.

    A retained workspace exists so the next attempt can reuse it, so one is removed
    only when its job will not run again, or when it has sat untouched past the
    retention window and its bytes are worth more than its diagnostics.
    """

    if retention_hours <= 0:
        raise ValueError("retention_hours must be positive")
    scratch_root = settings.scratch_root.expanduser()
    if not scratch_root.is_dir():
        return ()
    deadline = (now if now is not None else time.time()) - retention_hours * 3600
    removed: list[int] = []
    for directory in sorted(scratch_root.iterdir()):
        if not directory.is_dir() or not directory.name.startswith("job-"):
            continue
        try:
            job_id = int(directory.name.removeprefix("job-"))
        except ValueError:
            continue
        try:
            expired = directory.stat().st_mtime < deadline
        except OSError:
            continue
        if not expired and is_retainable(job_id):
            continue
        shutil.rmtree(directory, ignore_errors=True)
        removed.append(job_id)
    return tuple(removed)
