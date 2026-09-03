from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_ableton import ARCHIVE_ID, URL, VIDEO_ID, make_config

from audio_archive.integrity import verify_sha256sums, write_sha256sums
from audio_archive.manifest import write_manifest_atomic
from audio_archive.tooling import SubprocessRunner
from audio_archive.verify import sha256_file
from audio_archive.wav24 import Wav24Service

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _decode(path: Path, *, sample_format: str) -> bytes:
    return subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-c:a",
            f"pcm_{sample_format}",
            "-f",
            sample_format,
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def _probe(path: Path, entries: str) -> str:
    return subprocess.run(
        [
            str(FFPROBE),
            "-hide_banner",
            "-loglevel",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            entries,
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.split()


@unittest.skipUnless(FFMPEG and FFPROBE, "real FFmpeg and FFprobe are required")
class Wav24FfmpegTests(unittest.TestCase):
    def _item(self, config, *, duration: float, rate: int, channels: int) -> Path:
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
                f"sine=frequency=440:sample_rate={rate}:duration={duration}",
                "-ac",
                str(channels),
                "-c:a",
                "pcm_s16le",
                str(master),
            ],
            check=True,
        )
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
                "duration_seconds": duration,
            },
            "acquisition": {},
            "source_master": {
                "role": "source_master",
                "path": master_relative.as_posix(),
                "sha256": sha256_file(master),
                "sample_rate_hz": rate,
                "channels": channels,
            },
            "intermediates": [],
            "derivatives": [],
        }
        write_manifest_atomic(item / "metadata" / "archive.json", manifest)
        write_sha256sums(item, [master_relative, Path("metadata/archive.json")])
        return item

    def test_real_decode_is_24_bit_and_preserves_rate_and_channels(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            for fake_tool in config.tools_directory.iterdir():
                fake_tool.unlink()
            # 44.1 kHz mono proves the rate and layout are carried through rather
            # than normalized to the 48 kHz stereo default of the other fixtures.
            item = self._item(config, duration=2.0, rate=44100, channels=1)
            master = item / "master" / f"{VIDEO_ID}.wav"

            result = Wav24Service(config, SubprocessRunner()).create(item, job_id=1)

            self.assertFalse(result.segmented)
            output = result.assets[0].path
            codec, rate, channels = _probe(output, "stream=codec_name,sample_rate,channels")
            self.assertEqual(codec, "pcm_s24le")
            self.assertEqual(int(rate), 44100)
            self.assertEqual(int(channels), 1)
            self.assertEqual(_decode(output, sample_format="s24le"), _decode(master, sample_format="s24le"))
            self.assertTrue(verify_sha256sums(item).valid)

    def test_real_segmented_decode_is_gapless_and_sample_identical(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory), safe_size_gib=0.0008)
            for fake_tool in config.tools_directory.iterdir():
                fake_tool.unlink()
            item = self._item(config, duration=3.3, rate=48000, channels=2)
            master = item / "master" / f"{VIDEO_ID}.wav"

            result = Wav24Service(config, SubprocessRunner()).create(item, job_id=1)

            self.assertTrue(result.segmented)
            self.assertGreaterEqual(len(result.assets), 2)
            expected = _decode(master, sample_format="s24le")
            segmented = b"".join(
                _decode(asset.path, sample_format="s24le") for asset in result.assets
            )
            self.assertEqual(segmented, expected)
            self.assertEqual(result.assets[-1].end_sample * 2 * 3, len(expected))
            self.assertTrue(verify_sha256sums(item).valid)


if __name__ == "__main__":
    unittest.main()
