import unittest

from audio_archive.policy import GIB, estimate_pcm_bytes, plan_ableton_output


class OutputPolicyTests(unittest.TestCase):
    def test_one_hour_float_stereo_is_about_1_29_gib(self) -> None:
        size = estimate_pcm_bytes(duration_seconds=3600, sample_rate_hz=48000, channels=2)
        self.assertAlmostEqual(size / GIB, 1.287, places=3)

    def test_93_minutes_crosses_safe_threshold_and_segments(self) -> None:
        plan = plan_ableton_output(
            duration_seconds=93 * 60,
            sample_rate_hz=48000,
            channels=2,
        )
        self.assertTrue(plan.segmented)
        self.assertEqual(plan.segment_count, 2)
        self.assertEqual(plan.segment_seconds, 3600)

    def test_channel_policy_rejects_multichannel(self) -> None:
        with self.assertRaises(ValueError):
            estimate_pcm_bytes(duration_seconds=60, sample_rate_hz=48000, channels=6)

    def test_high_rate_segments_are_shortened_to_stay_below_safe_size(self) -> None:
        plan = plan_ableton_output(
            duration_seconds=3600,
            sample_rate_hz=96000,
            channels=2,
        )
        self.assertTrue(plan.segmented)
        self.assertLess(plan.segment_seconds, 3600)
        self.assertLess(plan.segment_seconds * 96000 * 2 * 4, 1.8 * GIB)


if __name__ == "__main__":
    unittest.main()
