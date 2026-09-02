from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from audio_archive.acquisition import AcquisitionRequest, AcquisitionService
from audio_archive.config import AppConfig
from audio_archive.db import ArchiveDatabase
from audio_archive.inputs import normalize_request
from audio_archive.integrity import verify_sha256sums
from audio_archive.models import JobState
from audio_archive.pipeline import acquire_ready_job
from audio_archive.tooling import CommandResult, ToolExecutionError


VIDEO_ID = "dQw4w9WgXcQ"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def make_config(root: Path) -> AppConfig:
    tools = root / "tools"
    tools.mkdir(parents=True)
    for name in ("yt-dlp", "ffmpeg", "ffprobe", "deno"):
        (tools / name).write_text("test tool", encoding="utf-8")
    provider_entry = tools / "bgutil-ytdlp-pot-provider" / "server" / "src" / "main.ts"
    provider_entry.parent.mkdir(parents=True)
    provider_entry.write_text("test provider", encoding="utf-8")
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


class FakeRunner:
    def __init__(
        self,
        *,
        combined: bool = False,
        warning: str = "",
        fail_download: bool = False,
    ):
        self.combined = combined
        self.warning = warning
        self.fail_download = fail_download
        self.commands: list[tuple[str, ...]] = []
        self.raw_info = b""

    def _result(self, argv: tuple[str, ...], stdout: str = "", stderr: str = "") -> CommandResult:
        return CommandResult(
            argv=argv,
            returncode=0,
            stdout=stdout,
            stderr=stderr,
            started_at_utc="2026-08-17T12:00:00+00:00",
            finished_at_utc="2026-08-17T12:00:01+00:00",
        )

    def run(self, argv, *, cwd=None) -> CommandResult:
        command = tuple(str(part) for part in argv)
        self.commands.append(command)
        tool = Path(command[0]).name
        if tool == "yt-dlp" and "--write-info-json" in command:
            if self.fail_download:
                result = CommandResult(
                    argv=command,
                    returncode=1,
                    stdout="",
                    stderr="ERROR: source is unavailable",
                    started_at_utc="2026-08-17T12:00:00+00:00",
                    finished_at_utc="2026-08-17T12:00:01+00:00",
                )
                raise ToolExecutionError(result)
            output_root = Path(command[command.index("--paths") + 1])
            output_root.mkdir(parents=True, exist_ok=True)
            selected = {
                "format_id": "251",
                "ext": "webm",
                "acodec": "opus",
                "vcodec": "vp9" if self.combined else "none",
                "abr": 128,
                "asr": 48000,
                "audio_channels": 2,
                "format_note": "medium",
            }
            info = {
                "id": VIDEO_ID,
                "title": "Test Artist - Test Song",
                "channel": "Test Artist",
                "duration": 180,
                "ext": "webm",
                "requested_downloads": [selected],
                "formats": [selected],
            }
            self.raw_info = (json.dumps(info, separators=(",", ":")) + "\n").encode()
            (output_root / "source.info.json").write_bytes(self.raw_info)
            (output_root / "source.webm").write_bytes(b"combined" if self.combined else b"audio")
            (output_root / "source.webp").write_bytes(b"thumbnail")
            return self._result(command, stderr=self.warning)
        if tool == "ffprobe":
            media = Path(command[-1])
            has_video = self.combined and media.name == "source.webm"
            streams = [
                {
                    "codec_type": "audio",
                    "codec_name": "opus",
                    "sample_rate": "48000",
                    "channels": 2,
                    "bit_rate": "128000",
                }
            ]
            if has_video:
                streams.append({"codec_type": "video", "codec_name": "vp9"})
            return self._result(
                command,
                stdout=json.dumps(
                    {
                        "format": {"format_name": "matroska,webm", "duration": "180.0"},
                        "streams": streams,
                    }
                ),
            )
        if tool == "ffmpeg" and "-i" in command:
            Path(command[-1]).write_bytes(b"demuxed audio")
            return self._result(command)
        versions = {
            "yt-dlp": "2026.07.04",
            "ffmpeg": "ffmpeg version 6.1.1",
            "ffprobe": "ffprobe version 6.1.1",
            "deno": "deno 2.4.0",
        }
        return self._result(command, stdout=versions.get(tool, "test") + "\n")


def request(job_id: int = 1, profile: str = "archive") -> AcquisitionRequest:
    return AcquisitionRequest(
        job_id=job_id,
        video_id=VIDEO_ID,
        url=URL,
        profile=profile,
        artist="Test Artist",
        title="Test Song",
        origin="url",
    )


class AcquisitionTests(unittest.TestCase):
    def test_audio_only_master_is_verified_published_and_reusable(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            runner = FakeRunner()
            service = AcquisitionService(config, runner)
            result = service.acquire(request())

            self.assertEqual(result.quality_status, "verified_best_available")
            self.assertTrue(result.master_path.is_file())
            self.assertTrue(verify_sha256sums(result.item_directory).valid)
            self.assertEqual(
                (result.item_directory / "metadata/source.info.json").read_bytes(),
                runner.raw_info,
            )
            yt_dlp_command = next(item for item in runner.commands if "--write-info-json" in item)
            self.assertIn("--ignore-config", yt_dlp_command)
            self.assertIn("--no-playlist", yt_dlp_command)
            self.assertIn("--js-runtimes", yt_dlp_command)
            self.assertTrue(
                yt_dlp_command[yt_dlp_command.index("--js-runtimes") + 1].startswith("deno:")
            )
            extractor_args = [
                yt_dlp_command[index + 1]
                for index, value in enumerate(yt_dlp_command)
                if value == "--extractor-args"
            ]
            self.assertFalse(
                any(value.startswith("youtube:player_client=") for value in extractor_args)
            )
            provider_arg = next(
                value for value in extractor_args if value.startswith("youtubepot-bgutilscript:")
            )
            self.assertIn("server_home=", provider_arg)
            self.assertIn("bgutil-ytdlp-pot-provider", provider_arg)
            self.assertEqual(yt_dlp_command[yt_dlp_command.index("--socket-timeout") + 1], "30")
            self.assertEqual(yt_dlp_command[yt_dlp_command.index("--retries") + 1], "3")
            self.assertEqual(yt_dlp_command[yt_dlp_command.index("--fragment-retries") + 1], "3")
            self.assertEqual(yt_dlp_command[yt_dlp_command.index("--extractor-retries") + 1], "2")
            self.assertEqual(yt_dlp_command[yt_dlp_command.index("--format") + 1], "bestaudio/best")
            self.assertNotIn("--extract-audio", yt_dlp_command)

            acquisition_calls = sum("--write-info-json" in item for item in runner.commands)
            for tool in config.tools_directory.iterdir():
                if tool.is_file():
                    tool.unlink()
            reused = service.acquire(request())
            self.assertTrue(reused.reused_existing)
            self.assertEqual(
                sum("--write-info-json" in item for item in runner.commands), acquisition_calls
            )

    def test_combined_fallback_is_demuxed_with_codec_copy(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            runner = FakeRunner(combined=True)
            result = AcquisitionService(config, runner).acquire(request())
            self.assertEqual(result.quality_status, "fallback_source")
            self.assertEqual(result.master_path.read_bytes(), b"demuxed audio")
            ffmpeg_command = next(item for item in runner.commands if "-c:a" in item)
            self.assertEqual(ffmpeg_command[ffmpeg_command.index("-c:a") + 1], "copy")
            self.assertIn("-vn", ffmpeg_command)

    def test_quality_warning_prevents_verified_best_claim(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            runner = FakeRunner(warning="WARNING: No supported JavaScript runtime was found")
            result = AcquisitionService(config, runner).acquire(request())
            self.assertEqual(result.quality_status, "best_available_with_warnings")
            self.assertEqual(result.warnings[0].category, "javascript_runtime")

    def test_missing_po_token_provider_fails_before_download(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            provider_root = config.tools_directory / "bgutil-ytdlp-pot-provider"
            for path in sorted(provider_root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            provider_root.rmdir()
            runner = FakeRunner()
            with self.assertRaisesRegex(FileNotFoundError, "PO token provider"):
                AcquisitionService(config, runner).acquire(request())
            self.assertFalse(any("--write-info-json" in item for item in runner.commands))

    def test_database_pipeline_completes_archive_profile(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            database = ArchiveDatabase(config.database_path)
            database.initialize()
            job_id = database.create_job(normalize_request(url=URL, profile="archive", origin="url"))
            result = acquire_ready_job(database, config, FakeRunner(), job_id)
            job = database.get_job(job_id)
            self.assertEqual(job["state"], JobState.COMPLETED.value)
            self.assertEqual(job["quality_status"], "verified_best_available")
            self.assertIsNotNone(database.find_archive_item("youtube", VIDEO_ID))
            self.assertEqual(result.video_id, VIDEO_ID)

    def test_non_archive_profile_advances_to_conversion_stage(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            database = ArchiveDatabase(config.database_path)
            database.initialize()
            job_id = database.create_job(normalize_request(url=URL, profile="ableton", origin="url"))
            acquire_ready_job(database, config, FakeRunner(), job_id)
            self.assertEqual(database.get_job(job_id)["state"], JobState.CONVERTING.value)

    def test_failed_download_is_recorded_and_retains_diagnostic_log(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            database = ArchiveDatabase(config.database_path)
            database.initialize()
            job_id = database.create_job(
                normalize_request(url=URL, profile="archive", origin="url")
            )
            with self.assertRaises(ToolExecutionError):
                acquire_ready_job(database, config, FakeRunner(fail_download=True), job_id)
            job = database.get_job(job_id)
            self.assertEqual(job["state"], JobState.FAILED.value)
            self.assertEqual(job["error_stage"], JobState.DOWNLOADING.value)
            log = config.temp_directory / str(job_id) / "ingest.log"
            self.assertTrue(log.is_file())
            self.assertIn("source is unavailable", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
