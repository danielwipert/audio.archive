from pathlib import Path
import unittest

from audio_archive.config import load_config, load_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_paths_are_portable_and_resolved_from_project_root(self) -> None:
        config = load_config(PROJECT_ROOT)
        self.assertEqual(config.archive_root, PROJECT_ROOT / "archive")
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.safe_wav_size_gib, 1.8)

    def test_known_profile_loads(self) -> None:
        profile = load_profile("ableton", PROJECT_ROOT)
        self.assertTrue(profile["source_master"])
        self.assertTrue(profile["ableton_intermediate"])
        self.assertFalse(profile["listening_derivative"])

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            load_profile("mystery", PROJECT_ROOT)


if __name__ == "__main__":
    unittest.main()

