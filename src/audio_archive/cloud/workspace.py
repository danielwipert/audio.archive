from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from ..config import AppConfig
from .config import CloudSettings
from .models import WorkerClaim


@dataclass(frozen=True)
class CloudWorkspace:
    """Job-attempt scratch space that is safe to delete after publication."""

    scratch_root: Path
    root: Path

    @classmethod
    def for_claim(cls, settings: CloudSettings, claim: WorkerClaim) -> "CloudWorkspace":
        scratch_root = settings.scratch_root.expanduser().resolve()
        root = (scratch_root / f"job-{claim.job_id}" / claim.claim_token.hex).resolve()
        if not root.is_relative_to(scratch_root):
            raise ValueError("Cloud workspace escaped the configured scratch root")
        return cls(scratch_root=scratch_root, root=root)

    @property
    def archive_root(self) -> Path:
        return self.root / "archive"

    @property
    def temp_directory(self) -> Path:
        return self.root / "temp"

    def prepare(self) -> None:
        self._assert_safe()
        if self.root.exists():
            shutil.rmtree(self.root)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.temp_directory.mkdir(parents=True, exist_ok=True)

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

    def cleanup(self) -> None:
        self._assert_safe()
        shutil.rmtree(self.root, ignore_errors=True)
        job_root = self.root.parent
        if job_root != self.scratch_root and job_root.exists():
            try:
                job_root.rmdir()
            except OSError:
                pass

    def _assert_safe(self) -> None:
        root = self.root.resolve()
        scratch = self.scratch_root.resolve()
        if root == scratch or not root.is_relative_to(scratch):
            raise ValueError("Refusing to modify an unsafe cloud workspace path")
