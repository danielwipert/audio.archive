import unittest

from audio_archive.policy import ABLETON_VARIANT, WAV24_VARIANT, PcmVariant, GIB, estimate_pcm_bytes, plan_ableton_output


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


class PcmVariantPlanTests(unittest.TestCase):
    def test_24_bit_output_is_three_quarters_the_size_of_32_bit_float(self) -> None:
        kwargs = {"duration_seconds": 3600.0, "sample_rate_hz": 48000, "channels": 2}
        float_plan = plan_ableton_output(**kwargs, bits_per_sample=ABLETON_VARIANT.bits_per_sample)
        int_plan = plan_ableton_output(**kwargs, bits_per_sample=WAV24_VARIANT.bits_per_sample)

        self.assertEqual(int_plan.estimated_bytes * 4, float_plan.estimated_bytes * 3)

    def test_24_bit_long_form_still_segments_beneath_the_safe_threshold(self) -> None:
        # Three hours of 24-bit stereo is 2.9 GiB, still far above the 1.8 GiB ceiling.
        plan = plan_ableton_output(
            duration_seconds=3 * 3600.0,
            sample_rate_hz=48000,
            channels=2,
            bits_per_sample=24,
        )

        self.assertTrue(plan.segmented)
        self.assertGreaterEqual(plan.segment_count, 2)

    def test_a_variant_must_describe_a_whole_number_of_bytes_per_sample(self) -> None:
        with self.assertRaises(ValueError):
            PcmVariant(
                role="broken",
                codec="pcm_s20le",
                bits_per_sample=20,
                manifest_section="derivatives",
                output_subpath="derivatives/broken",
                log_name="convert-broken.log",
                label="Broken",
            )
