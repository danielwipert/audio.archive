import unittest

from audio_archive.warnings import classify_warnings


class WarningTests(unittest.TestCase):
    def test_quality_limiting_warnings_are_classified(self) -> None:
        warnings = classify_warnings(
            "",
            "WARNING: No supported JavaScript runtime was found\n"
            "WARNING: Some formats may be missing because a PO token was not provided",
        )
        self.assertEqual([item.category for item in warnings], ["javascript_runtime", "po_token"])
        self.assertTrue(all(item.quality_affecting for item in warnings))

    def test_unrelated_warning_is_preserved_without_quality_claim(self) -> None:
        warnings = classify_warnings("WARNING: Thumbnail conversion was skipped", "")
        self.assertEqual(warnings[0].category, "other")
        self.assertFalse(warnings[0].quality_affecting)


if __name__ == "__main__":
    unittest.main()

