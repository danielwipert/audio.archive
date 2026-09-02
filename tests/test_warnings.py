import unittest

from audio_archive.warnings import classify_source_access_failure, classify_warnings


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

    def test_error_line_is_always_quality_affecting(self) -> None:
        warnings = classify_warnings("", "ERROR: extractor returned incomplete metadata")
        self.assertTrue(warnings[0].quality_affecting)


class SourceAccessFailureTests(unittest.TestCase):
    def test_rate_limited_egress_is_named_ahead_of_the_bot_challenge(self) -> None:
        stderr = (
            "WARNING: [youtube] tE0PSlNVN0Q: Unable to download webpage: "
            "HTTP Error 429: Too Many Requests\n"
            "ERROR: [youtube] tE0PSlNVN0Q: Sign in to confirm you're not a bot."
        )
        self.assertEqual(classify_source_access_failure("", stderr), "SourceAccessRateLimited")

    def test_bot_challenge_alone_is_classified(self) -> None:
        self.assertEqual(
            classify_source_access_failure("", "ERROR: Sign in to confirm you're not a bot"),
            "SourceAccessBotCheck",
        )

    def test_forbidden_and_token_and_unavailable_sources_are_distinct(self) -> None:
        self.assertEqual(
            classify_source_access_failure("", "ERROR: unable to download: HTTP Error 403"),
            "SourceAccessForbidden",
        )
        self.assertEqual(
            classify_source_access_failure("", "ERROR: Could not get a GVS PO Token"),
            "SourceAccessTokenFailure",
        )
        self.assertEqual(
            classify_source_access_failure("", "ERROR: Video unavailable"),
            "SourceUnavailable",
        )

    def test_media_processing_failure_is_not_a_source_access_failure(self) -> None:
        self.assertIsNone(
            classify_source_access_failure(
                "", "Error opening output file: Invalid data found when processing input"
            )
        )


if __name__ == "__main__":
    unittest.main()
