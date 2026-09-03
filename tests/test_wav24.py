from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_ableton import AbletonRunner, VIDEO_ID, make_archive_item, make_config

from audio_archive.ableton import AbletonService
from audio_archive.integrity import verify_sha256sums
from audio_archive.wav24 import Wav24Service


def _manifest(item: Path) -> dict[str, object]:
    return json.loads((item / "metadata" / "archive.json").read_text(encoding="utf-8"))


class Wav24Tests(unittest.TestCase):
    def test_output_is_24_bit_and_recorded_as_a_derivative(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            item, master_sha256 = make_archive_item(config, duration_seconds=200.0)
            runner = AbletonRunner(200.0, output_codec="pcm_s24le")

            result = Wav24Service(config, runner).create(item, job_id=7)

            self.assertFalse(result.segmented)
            asset = result.assets[0]
            self.assertEqual(asset.relative_path, f"derivatives/wav24/{VIDEO_ID}.wav")
            self.assertEqual(asset.role, "wav24")
            self.assertEqual(asset.audio_format, "pcm_s24le")

            convert = next(
                command
                for command in runner.commands
                if Path(command[0]).name == "ffmpeg" and "-version" not in command
            )
            self.assertIn("pcm_s24le", convert)
            self.assertNotIn("pcm_f32le", convert)
            # No processing may be introduced on the way to an integer format.
            for forbidden in ("-af", "-filter:a", "-ar", "-ac", "dither"):
                self.assertNotIn(forbidden, convert)

            manifest = _manifest(item)
            self.assertEqual(manifest["intermediates"], [])
            record = manifest["derivatives"][0]
            self.assertEqual(record["role"], "wav24")
            self.assertEqual(record["audio_format"], "pcm_s24le")
            self.assertEqual(record["source_sha256"], master_sha256)
            self.assertFalse(record["resampled"])
            self.assertFalse(record["dithered"])
            self.assertFalse(record["normalization"])
            self.assertTrue(verify_sha256sums(item).valid)
            self.assertTrue((item / "logs" / "convert-wav24.log").is_file())

    def test_existing_output_is_reused_without_reconverting(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            item, _ = make_archive_item(config, duration_seconds=200.0)
            Wav24Service(config, AbletonRunner(200.0, output_codec="pcm_s24le")).create(
                item, job_id=7
            )

            second = AbletonRunner(200.0, output_codec="pcm_s24le")
            result = Wav24Service(config, second).create(item, job_id=8)

            self.assertTrue(result.reused_existing)
            self.assertFalse(
                any(
                    Path(command[0]).name == "ffmpeg" and "-version" not in command
                    for command in second.commands
                )
            )

    def test_both_wav_variants_coexist_for_one_item(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            item, _ = make_archive_item(config, duration_seconds=200.0)

            AbletonService(config, AbletonRunner(200.0)).create(item, job_id=7)
            Wav24Service(config, AbletonRunner(200.0, output_codec="pcm_s24le")).create(
                item, job_id=7
            )

            manifest = _manifest(item)
            self.assertEqual([r["role"] for r in manifest["intermediates"]], ["ableton"])
            self.assertEqual([r["role"] for r in manifest["derivatives"]], ["wav24"])
            self.assertTrue((item / "intermediates" / "ableton" / f"{VIDEO_ID}.wav").is_file())
            self.assertTrue((item / "derivatives" / "wav24" / f"{VIDEO_ID}.wav").is_file())
            self.assertTrue((item / "logs" / "convert.log").is_file())
            self.assertTrue((item / "logs" / "convert-wav24.log").is_file())
            self.assertTrue(verify_sha256sums(item).valid)

    def test_long_form_output_is_segmented_under_the_safe_size(self) -> None:
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory), safe_size_gib=0.05)
            item, _ = make_archive_item(config, duration_seconds=3600.0)

            result = Wav24Service(
                config, AbletonRunner(3600.0, output_codec="pcm_s24le")
            ).create(item, job_id=7)

            self.assertTrue(result.segmented)
            self.assertGreaterEqual(len(result.assets), 2)
            starts = [asset.start_sample for asset in result.assets]
            self.assertEqual(starts, sorted(starts))
            for earlier, later in zip(result.assets, result.assets[1:]):
                self.assertEqual(earlier.end_sample, later.start_sample)
            self.assertTrue(
                all(
                    asset.relative_path.startswith("derivatives/wav24/segments/")
                    for asset in result.assets
                )
            )
            self.assertTrue(verify_sha256sums(item).valid)


if __name__ == "__main__":
    unittest.main()
