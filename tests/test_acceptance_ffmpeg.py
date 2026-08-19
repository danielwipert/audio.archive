from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_ableton import make_config

from audio_archive.acceptance import create_ableton_acceptance_fixtures
from audio_archive.integrity import verify_sha256sums
from audio_archive.tooling import SubprocessRunner

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "real FFmpeg and FFprobe are required")
class AcceptanceFixtureTests(unittest.TestCase):
    def test_generator_creates_verified_normal_and_segmented_ableton_outputs(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            for fake_tool in config.tools_directory.iterdir():
                fake_tool.unlink()

            result = create_ableton_acceptance_fixtures(config, SubprocessRunner())

            normal = result["normal"]
            segmented = result["segmented"]
            self.assertFalse(normal["segmented"])
            self.assertEqual(normal["segments"], 1)
            self.assertTrue(Path(normal["paths"][0]).is_file())
            self.assertTrue(segmented["segmented"])
            self.assertGreaterEqual(segmented["segments"], 2)
            self.assertTrue(all(Path(path).is_file() for path in segmented["paths"]))
            self.assertTrue(verify_sha256sums(Path(normal["item_directory"])).valid)
            self.assertTrue(verify_sha256sums(Path(segmented["item_directory"])).valid)


if __name__ == "__main__":
    unittest.main()
