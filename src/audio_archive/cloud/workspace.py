from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from ..config import AppConfig


@dataclass(frozen=True)
class CloudJobWorkspace:
    """Job-isolated ephemeral filesystem used by the cloud worker."""

    scratch_root: Path
    job_id: int

    def __post_init__(self) -> None:
        if self.job_id <= 0:
            raise ValueError("job_id must be positive")

    @property
    def root(self) -> Path:
        return self.scratch_root / str(self.job_id)

    @property
    def archive_root(self) -> Path:
        return self.root / "archive"

    @property
    def temp_directory(self) -> Path:
        return self.root / "temp"

    def prepare(self) -> None:
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        root = self.root
        resolved_root = root.resolve()
        resolved_scratch = self.scratch_root.resolve()
        if resolved_root.parent != resolved_scratch:
            raise ValueError("Cloud workspace escaped the configured scratch root")
        root.mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.temp_directory.mkdir(parents=True, exist_ok=True)

    def app_config(self, base: AppConfig) -> AppConfig:
        self.prepare()
        return replace(
            base,
            archive_root=self.archive_root,
            temp_directory=self.temp_directory,
            database_path=self.root / "unused-local.db",
            host="127.0.0.1",
            open_browser=False,
        )

    def cleanup(self) -> None:
        root = self.root
        if not root.exists():
            return
        resolved_root = root.resolve()
        resolved_scratch = self.scratch_root.resolve()
        if resolved_root.parent != resolved_scratch:
            raise ValueError("Refusing to delete workspace outside configured scratch root")
        shutil.rmtree(root)
