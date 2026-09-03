from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from audio_archive.ableton import AbletonService
from audio_archive.config import AppConfig
from audio_archive.db import ArchiveDatabase
from audio_archive.integrity import verify_sha256sums, write_sha256sums
from audio_archive.manifest import write_manifest_atomic
from audio_archive.models import JobRequest, JobState
from audio_archive.pipeline import create_ableton_for_job
from audio_archive.tooling import CommandResult
from audio_archive.verify import sha256_file

VIDEO_ID = "dQw4w9WgXcQ"
ARCHIVE_ID = f"youtube:{VIDEO_ID}"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def make_config(root: Path, *, safe_size_gib: float = 1.8) -> AppConfig:
    tools = root / "tools"
    tools.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        (tools / name).write_text("test tool", encoding="utf-8")
    archive = root / "archive"
    return AppConfig(
        project_root=root,
        archive_root=archive,
        temp_directory=archive / "temp",
        database_path=archive / "app-data" / "archive.db",
        host="127.0.0.1",
        port=8765,
        open_browser=False,
        poll_interval_seconds=2.0,
        safe_wav_size_gib=safe_size_gib,
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


def make_archive_item(config: AppConfig, *, duration_seconds: float) -> tuple[Path, str]:
    item = config.archive_root / "items" / "youtube" / VIDEO_ID
    master_relative = Path("master") / f"{VIDEO_ID}.webm"
    master = item / master_relative
    master.parent.mkdir(parents=True)
    master.write_bytes(b"verified native source")
    artwork_relative = Path("artwork/source-thumbnail.jpg")
    artwork = item / artwork_relative
    artwork.parent.mkdir(parents=True)
    artwork.write_bytes(b"verified source thumbnail")
    master_sha256 = sha256_file(master)
    manifest = {
        "schema_version": "1.2",
        "archive_id": ARCHIVE_ID,
        "content_type": "song",
        "request": {"origin": "url", "profile": "ableton"},
        "resolution": {},
        "source": {
            "platform": "youtube",
            "id": VIDEO_ID,
            "url": URL,
            "title": "Test source",
            "creator": "Test creator",
            "duration_seconds": duration_seconds,
        },
        "acquisition": {},
        "source_master": {
            "role": "source_master",
            "path": master_relative.as_posix(),
            "sha256": master_sha256,
            "sample_rate_hz": 48000,
            "channels": 2,
        },
        "intermediates": [],
        "derivatives": [],
    }
    manifest_path = item / "metadata" / "archive.json"
    write_manifest_atomic(manifest_path, manifest)
    write_sha256sums(
        item,
        [master_relative, artwork_relative, Path("metadata/archive.json")],
    )
    return item, master_sha256


class AbletonRunner:
    def __init__(
        self,
        duration_seconds: float,
        *,
        sample_rate_hz: int = 48000,
        channels: int = 2,
        output_codec: str = "pcm_f32le",
    ):
        self.duration_seconds = duration_seconds
        self.sample_rate_hz = sample_rate_hz
        self.channels = channels
        self.output_codec = output_codec
        self.commands: list[tuple[str, ...]] = []
        self.output_samples: dict[str, int] = {}

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
            total_samples = round(self.duration_seconds * self.sample_rate_hz)
            if "segment" in command:
                seconds = int(command[command.index("-segment_time") + 1])
                samples_per_segment = seconds * self.sample_rate_hz
                pattern = command[-1]
                remaining = total_samples
                position = 1
                while remaining:
                    count = min(samples_per_segment, remaining)
                    output = Path(pattern.replace("%03d", f"{position:03d}"))
                    output.write_bytes(f"segment {position}".encode())
                    self.output_samples[output.name] = count
                    remaining -= count
                    position += 1
            else:
                output = Path(command[-1])
                output.write_bytes(b"float wav")
                self.output_samples[output.name] = total_samples
            return self._result(command)
        if tool == "ffprobe":
            media = Path(command[-1])
            is_output = media.suffix == ".wav"
            sample_count = self.output_samples.get(
                media.name, round(self.duration_seconds * self.sample_rate_hz)
            )
            codec = self.output_codec if is_output else "opus"
            format_name = "wav" if is_output else "matroska,webm"
            return self._result(
                command,
                stdout=json.dumps(
                    {
                        "format": {
                            "format_name": format_name,
                            "duration": str(sample_count / self.sample_rate_hz),
                        },
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": codec,
                                "sample_rate": str(self.sample_rate_hz),
                                "channels": self.channels,
                                "duration_ts": sample_count,
                                "time_base": f"1/{self.sample_rate_hz}",
                            }
                        ],
                    }
                ),
            )
        raise AssertionError(command)


class AbletonTests(unittest.TestCase):
    def test_normal_item_creates_verified_float_wav_and_reuses_it(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            item, source_sha256 = make_archive_item(config, duration_seconds=180)
            runner = AbletonRunner(180)
            result = AbletonService(config, runner).create(item, job_id=1)

            self.assertFalse(result.segmented)
            self.assertFalse(result.reused_existing)
            self.assertEqual(len(result.assets), 1)
            self.assertEqual(result.assets[0].sample_count, 180 * 48000)
            self.assertTrue(verify_sha256sums(item).valid)
            manifest = json.loads((item / "metadata/archive.json").read_text(encoding="utf-8"))
            record = manifest["intermediates"][0]
            self.assertEqual(record["audio_format"], "pcm_f32le")
            self.assertEqual(record["source_sha256"], source_sha256)
            self.assertFalse(record["normalization"])
            conversion = next(command for command in runner.commands if "pcm_f32le" in command)
            self.assertNotIn("-ar", conversion)
            self.assertNotIn("-ac", conversion)
            self.assertNotIn("-af", conversion)

            conversion_count = sum("pcm_f32le" in command for command in runner.commands)
            reused = AbletonService(config, runner).create(item, job_id=2)
            self.assertTrue(reused.reused_existing)
            self.assertEqual(
                sum("pcm_f32le" in command for command in runner.commands),
                conversion_count,
            )

    def test_long_item_creates_ordered_gapless_segments(self) -> None:
        duration = 93 * 60
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            item, _ = make_archive_item(config, duration_seconds=duration)
            result = AbletonService(config, AbletonRunner(duration)).create(item, job_id=1)

            self.assertTrue(result.segmented)
            self.assertEqual(len(result.assets), 2)
            self.assertEqual(result.assets[0].segment_index, 1)
            self.assertEqual(result.assets[1].segment_index, 2)
            self.assertEqual(result.assets[0].end_sample, result.assets[1].start_sample)
            self.assertEqual(result.assets[-1].end_sample, duration * 48000)
            self.assertTrue(verify_sha256sums(item).valid)

    def test_pipeline_records_asset_and_completes_ableton_job(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            item, master_sha256 = make_archive_item(config, duration_seconds=180)
            database = ArchiveDatabase(config.database_path)
            database.initialize()
            job_id = database.create_job(JobRequest(url=URL, origin="url", profile="ableton"))
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

            create_ableton_for_job(database, config, AbletonRunner(180), job_id)

            job = database.get_job(job_id)
            self.assertEqual(job["state"], JobState.COMPLETED.value)
            self.assertEqual(job["progress_percent"], 100)
            roles = [row["role"] for row in database.list_assets(ARCHIVE_ID)]
            self.assertEqual(roles, ["ableton", "source_master"])

            complete_job = database.create_job(
                JobRequest(url=URL, origin="url", profile="complete")
            )
            database.transition_job(complete_job, JobState.DOWNLOADING)
            database.transition_job(complete_job, JobState.VERIFYING_MASTER)
            database.record_acquisition(
                complete_job,
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
            database.transition_job(complete_job, JobState.CONVERTING)
            reused = create_ableton_for_job(
                database,
                config,
                AbletonRunner(180),
                complete_job,
            )
            self.assertTrue(reused.reused_existing)
            self.assertEqual(
                database.get_job(complete_job)["state"],
                JobState.CONVERTING.value,
            )


if __name__ == "__main__":
    unittest.main()
