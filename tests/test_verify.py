from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from audio_archive.integrity import verify_sha256sums, write_sha256sums
from audio_archive.verify import parse_ffprobe


class VerifyTests(unittest.TestCase):
    def test_ffprobe_requires_one_audio_stream_and_rejects_video_for_master(self) -> None:
        data = {
            "format": {"format_name": "matroska,webm", "duration": "120.5"},
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "opus",
                    "sample_rate": "48000",
                    "channels": 2,
                    "bit_rate": "128000",
                },
                {"codec_type": "video", "codec_name": "vp9"},
            ],
        }
        with self.assertRaises(ValueError):
            parse_ffprobe(data, allow_video=False)
        probe = parse_ffprobe(data, allow_video=True)
        self.assertEqual(probe.video_stream_count, 1)
        self.assertEqual(probe.audio.sample_rate_hz, 48000)

    def test_checksum_verification_detects_change(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "master" / "item.webm"
            asset.parent.mkdir()
            asset.write_bytes(b"original")
            write_sha256sums(root, [Path("master/item.webm")])
            self.assertTrue(verify_sha256sums(root).valid)
            asset.write_bytes(b"changed")
            result = verify_sha256sums(root)
            self.assertFalse(result.valid)
            self.assertIn("checksum mismatch: master/item.webm", result.errors)


if __name__ == "__main__":
    unittest.main()

