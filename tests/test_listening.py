from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_ableton import (
    ARCHIVE_ID,
    URL,
    VIDEO_ID,
    AbletonRunner,
    make_archive_item,
    make_config,
)

from audio_archive.db import ArchiveDatabase
from audio_archive.integrity import verify_sha256sums
from audio_archive.listening import ListeningService
from audio_archive.models import JobRequest, JobState
from audio_archive.pipeline import create_ableton_for_job, create_listening_for_job
from audio_archive.tooling import CommandResult


class ListeningRunner:
    def __init__(self, duration_seconds: float = 180):
        self.duration_seconds = duration_seconds
        self.commands: list[tuple[str, ...]] = []

    def _result(self, command: tuple[str, ...], *, stdout: str = "") -> CommandResult:
        return CommandResult(
            argv=command,
            returncode=0,
            stdout=stdout,
            stderr="",
            started_at_utc="2026-08-17T12:00:00+00:00",
            finished_at_utc="2026-08-17T12:00:01+00:00",
        )

    def run(self, argv, *, cwd=None) -> CommandResult:
        command = tuple(str(part) for part in argv)
        self.commands.append(command)
        tool = Path(command[0]).name
        if "-version" in command:
            return self._result(command, stdout=f"{tool} version 7.1\n")
        if tool == "ffmpeg":
            Path(command[-1]).write_bytes(b"verified vbr mp3 with artwork")
            return self._result(command)
        if tool == "ffprobe":
            output = Path(command[-1]).suffix.casefold() == ".mp3"
            streams = [
                {
                    "codec_type": "audio",
                    "codec_name": "mp3" if output else "opus",
                    "sample_rate": "48000",
                    "channels": 2,
                    "bit_rate": "245000" if output else "128000",
                }
            ]
            if output:
                streams.append(
                    {
                        "codec_type": "video",
                        "codec_name": "mjpeg",
                        "disposition": {"attached_pic": 1},
                    }
                )
            return self._result(
                command,
                stdout=json.dumps(
                    {
                        "format": {
                            "format_name": "mp3" if output else "matroska,webm",
                            "duration": str(self.duration_seconds),
                            "tags": (
                                {"title": "Test source", "artist": "Test creator"}
                                if output
                                else {}
                            ),
                        },
                        "streams": streams,
                    }
                ),
            )
        raise AssertionError(command)


def prepare_job(
    database: ArchiveDatabase,
    item: Path,
    master_sha256: str,
    *,
    profile: str,
) -> int:
    job_id = database.create_job(JobRequest(url=URL, origin="url", profile=profile))
    database.transition_job(job_id, JobState.DOWNLOADING)
    database.transition_job(job_id, JobState.VERIFYING_MASTER)
    database.record_acquisition(
        job_id,
        archive_id=ARCHIVE_ID,
        source_id=VIDEO_ID,
        source_title="Test source",
        source_creator="Test creator",
        item_directory=str(item),
        manifest_path=str(item / "metadata/archive.json"),
        quality_status="verified_best_available",
        master_relative_path=f"master/{VIDEO_ID}.webm",
        master_sha256=master_sha256,
        media_properties={"sample_rate_hz": 48000, "channels": 2},
        warnings=[],
    )
    database.transition_job(job_id, JobState.CONVERTING)
    return job_id


class ListeningTests(unittest.TestCase):
    def test_creates_verified_mp3_from_local_master_and_reuses_it(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            item, source_sha256 = make_archive_item(config, duration_seconds=180)
            runner = ListeningRunner()

            result = ListeningService(config, runner).create(item, job_id=1)

            self.assertFalse(result.reused_existing)
            self.assertTrue(result.asset.path.is_file())
            self.assertTrue(verify_sha256sums(item).valid)
            manifest = json.loads((item / "metadata/archive.json").read_text(encoding="utf-8"))
            record = manifest["derivatives"][0]
            self.assertEqual(record["role"], "listening")
            self.assertEqual(record["source_sha256"], source_sha256)
            self.assertEqual(record["encoder_settings"]["quality_scale"], 0)
            self.assertTrue(record["embedded_artwork"])
            conversion = next(command for command in runner.commands if "libmp3lame" in command)
            self.assertIn(str(item / f"master/{VIDEO_ID}.webm"), conversion)
            self.assertIn(str(item / "artwork/source-thumbnail.jpg"), conversion)
            self.assertIn("-q:a", conversion)
            self.assertEqual(conversion[conversion.index("-q:a") + 1], "0")
            self.assertNotIn("yt-dlp", conversion)

            conversion_count = sum("libmp3lame" in command for command in runner.commands)
            reused = ListeningService(config, runner).create(item, job_id=2)
            self.assertTrue(reused.reused_existing)
            self.assertEqual(
                sum("libmp3lame" in command for command in runner.commands),
                conversion_count,
            )

    def test_listen_profile_records_asset_and_completes(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            item, master_sha256 = make_archive_item(config, duration_seconds=180)
            database = ArchiveDatabase(config.database_path)
            database.initialize()
            job_id = prepare_job(database, item, master_sha256, profile="listen")

            create_listening_for_job(database, config, ListeningRunner(), job_id)

            self.assertEqual(database.get_job(job_id)["state"], JobState.COMPLETED.value)
            roles = [row["role"] for row in database.list_assets(ARCHIVE_ID)]
            self.assertEqual(roles, ["listening", "source_master"])

    def test_complete_profile_finishes_only_after_both_derivatives(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            item, master_sha256 = make_archive_item(config, duration_seconds=180)
            database = ArchiveDatabase(config.database_path)
            database.initialize()
            job_id = prepare_job(database, item, master_sha256, profile="complete")

            create_ableton_for_job(database, config, AbletonRunner(180), job_id)
            self.assertEqual(database.get_job(job_id)["state"], JobState.CONVERTING.value)

            create_listening_for_job(database, config, ListeningRunner(), job_id)
            self.assertEqual(database.get_job(job_id)["state"], JobState.COMPLETED.value)
            roles = [row["role"] for row in database.list_assets(ARCHIVE_ID)]
            self.assertEqual(roles, ["ableton", "listening", "source_master"])


if __name__ == "__main__":
    unittest.main()
