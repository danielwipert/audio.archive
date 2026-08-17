from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_ableton import ARCHIVE_ID, URL, VIDEO_ID, make_config

from audio_archive.ableton import AbletonService
from audio_archive.integrity import verify_sha256sums, write_sha256sums
from audio_archive.manifest import write_manifest_atomic
from audio_archive.tooling import SubprocessRunner
from audio_archive.verify import sha256_file

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "real FFmpeg and FFprobe are required")
class AbletonFfmpegTests(unittest.TestCase):
    def test_real_segmented_decode_is_gapless_and_sample_identical(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root, safe_size_gib=0.001)
            for fake_tool in config.tools_directory.iterdir():
                fake_tool.unlink()
            item = config.archive_root / "items" / "youtube" / VIDEO_ID
            master_relative = Path("master") / f"{VIDEO_ID}.wav"
            master = item / master_relative
            master.parent.mkdir(parents=True)
            subprocess.run(
                [
                    str(FFMPEG),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=3.3",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    str(master),
                ],
                check=True,
            )
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
                    "title": "Generated test tone",
                    "creator": "test",
                    "duration_seconds": 3.3,
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
            write_sha256sums(item, [master_relative, Path("metadata/archive.json")])

            result = AbletonService(config, SubprocessRunner()).create(item, job_id=1)

            self.assertTrue(result.segmented)
            self.assertGreaterEqual(len(result.assets), 2)
            self.assertTrue(verify_sha256sums(item).valid)
            expected_pcm = subprocess.run(
                [
                    str(FFMPEG),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(master),
                    "-map",
                    "0:a:0",
                    "-c:a",
                    "pcm_f32le",
                    "-f",
                    "f32le",
                    "-",
                ],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            segmented_pcm = b"".join(
                subprocess.run(
                    [
                        str(FFMPEG),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(asset.path),
                        "-map",
                        "0:a:0",
                        "-f",
                        "f32le",
                        "-",
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                ).stdout
                for asset in result.assets
            )
            self.assertEqual(segmented_pcm, expected_pcm)
            self.assertEqual(result.assets[-1].end_sample * 2 * 4, len(expected_pcm))


if __name__ == "__main__":
    unittest.main()
