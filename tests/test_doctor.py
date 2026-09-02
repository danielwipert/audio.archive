from __future__ import annotations

import unittest
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory

from audio_archive.config import AppConfig
from audio_archive.doctor import run_doctor
from audio_archive.tooling import CommandResult


def make_config(root: Path) -> AppConfig:
    tools = root / "tools"
    tools.mkdir()
    for name in ("yt-dlp", "deno", "ffmpeg", "ffprobe"):
        (tools / name).write_text("test tool", encoding="utf-8")
    archive_root = root / "archive"
    archive_root.mkdir()
    return AppConfig(
        project_root=root,
        archive_root=archive_root,
        temp_directory=archive_root / "temp",
        database_path=archive_root / "app-data" / "archive.db",
        host="127.0.0.1",
        port=8765,
        open_browser=False,
        poll_interval_seconds=2.0,
        safe_wav_size_gib=1.8,
        segment_minutes=60,
        tools_directory=tools,
        yt_dlp="yt-dlp",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        deno="deno",
        candidate_limit=5,
        auto_select_min_score=90,
        auto_select_min_margin=15,
        max_csv_bytes=5 * 1024 * 1024,
    )


PINNED_YT_DLP = "2026.8.19"


def installed_version(name: str) -> str:
    return {"yt-dlp": PINNED_YT_DLP, "yt-dlp-ejs": "0.4.0"}[name]


class VersionRunner:
    def __init__(self, *, deno_version: str = "deno 2.4.3", yt_dlp_version: str = "2026.08.19"):
        self.deno_version = deno_version
        self.yt_dlp_version = yt_dlp_version

    def run(self, argv, *, cwd=None) -> CommandResult:
        command = tuple(str(part) for part in argv)
        tool = Path(command[0]).stem.lower()
        versions = {
            "yt-dlp": f"{self.yt_dlp_version}\n",
            "deno": f"{self.deno_version}\n",
            "ffmpeg": "ffmpeg version 7.1\n",
            "ffprobe": "ffprobe version 7.1\n",
        }
        return CommandResult(
            argv=command,
            returncode=0,
            stdout=versions[tool],
            stderr="",
            started_at_utc="2026-08-17T12:00:00+00:00",
            finished_at_utc="2026-08-17T12:00:01+00:00",
        )


class DoctorTests(unittest.TestCase):
    def test_complete_supported_toolchain_is_ready(self) -> None:
        with TemporaryDirectory() as directory:
            report = run_doctor(
                make_config(Path(directory)),
                VersionRunner(),
                distribution_version=installed_version,
            )

        self.assertTrue(report.ready)
        self.assertTrue(all(item.ok for item in report.diagnostics))

    def test_old_deno_blocks_readiness(self) -> None:
        with TemporaryDirectory() as directory:
            report = run_doctor(
                make_config(Path(directory)),
                VersionRunner(deno_version="deno 2.4.2"),
                distribution_version=installed_version,
            )

        deno = next(item for item in report.diagnostics if item.name == "Deno")
        self.assertFalse(report.ready)
        self.assertFalse(deno.ok)
        self.assertIn("2.4.3", deno.message)

    def test_yt_dlp_executable_must_match_the_installed_pin(self) -> None:
        with TemporaryDirectory() as directory:
            report = run_doctor(
                make_config(Path(directory)),
                VersionRunner(yt_dlp_version="2026.07.04"),
                distribution_version=installed_version,
            )

        yt_dlp = next(item for item in report.diagnostics if item.name == "yt-dlp")
        self.assertFalse(report.ready)
        self.assertFalse(yt_dlp.ok)
        self.assertIn(PINNED_YT_DLP, yt_dlp.message)

    def test_missing_ejs_components_block_readiness(self) -> None:
        def missing_distribution(name: str) -> str:
            if name == "yt-dlp-ejs":
                raise metadata.PackageNotFoundError
            return installed_version(name)

        with TemporaryDirectory() as directory:
            report = run_doctor(
                make_config(Path(directory)),
                VersionRunner(),
                distribution_version=missing_distribution,
            )

        ejs = next(item for item in report.diagnostics if item.name == "yt-dlp EJS")
        self.assertFalse(report.ready)
        self.assertFalse(ejs.ok)
        self.assertIn("missing", ejs.message)


if __name__ == "__main__":
    unittest.main()
