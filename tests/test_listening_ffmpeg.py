from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_ableton import ARCHIVE_ID, URL, VIDEO_ID, make_config

from audio_archive.integrity import verify_sha256sums, write_sha256sums
from audio_archive.listening import ListeningService
from audio_archive.manifest import write_manifest_atomic
from audio_archive.tooling import SubprocessRunner
from audio_archive.verify import probe_media, sha256_file

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "real FFmpeg and FFprobe are required")
class ListeningFfmpegTests(unittest.TestCase):
    def test_real_mp3_has_vbr_audio_curated_tags_and_attached_artwork(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
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
                    "sine=frequency=440:sample_rate=48000:duration=1.1",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    str(master),
                ],
                check=True,
            )
            artwork_relative = Path("artwork/source-thumbnail.jpg")
            artwork = item / artwork_relative
            artwork.parent.mkdir(parents=True)
            subprocess.run(
                [
                    str(FFMPEG),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=320x320",
                    "-frames:v",
                    "1",
                    str(artwork),
                ],
                check=True,
            )
            master_sha256 = sha256_file(master)
            manifest = {
                "schema_version": "1.2",
                "archive_id": ARCHIVE_ID,
                "content_type": "song",
                "request": {
                    "origin": "url",
                    "profile": "listen",
                    "artist": "Curated Artist",
                    "title": "Curated Title",
                    "version": "Archive Mix",
                },
                "resolution": {},
                "source": {
                    "platform": "youtube",
                    "id": VIDEO_ID,
                    "url": URL,
                    "title": "Generated test tone",
                    "creator": "test",
                    "duration_seconds": 1.1,
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

            result = ListeningService(config, SubprocessRunner()).create(item, job_id=1)

            self.assertTrue(verify_sha256sums(item).valid)
            probe = probe_media(SubprocessRunner(), str(FFPROBE), result.asset.path, allow_video=True)
            self.assertEqual(probe.audio.codec, "mp3")
            self.assertEqual(probe.audio.sample_rate_hz, 48000)
            self.assertEqual(probe.attached_picture_count, 1)
            self.assertEqual(probe.tags["title"], "Curated Title (Archive Mix)")
            self.assertEqual(probe.tags["artist"], "Curated Artist")
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))["derivatives"][0]
            self.assertEqual(saved["encoder"], "libmp3lame")
            self.assertEqual(saved["encoder_settings"], {"quality_mode": "VBR", "quality_scale": 0})


if __name__ == "__main__":
    unittest.main()
