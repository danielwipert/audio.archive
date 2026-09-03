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


class TransportAndExtractionTests(unittest.TestCase):
    def test_a_truncated_or_non_tls_response_is_named_as_transport(self) -> None:
        """Job 10's warnings, verbatim. They were all landing in "other", which told
        the reader nothing about a proxy returning corrupt responses."""

        warnings = classify_warnings(
            "",
            "[download] Got error: [SSL: WRONG_VERSION_NUMBER] wrong version number "
            "(_ssl.c:2590). Retrying (1/3)...\n"
            "WARNING: [youtube] unable to extract yt initial data\n"
            "WARNING: [youtube] Incomplete data received in embedded initial data; "
            "re-fetching using API.",
        )

        # One TLS-level failure, then the two messages describing the truncated page
        # and the fallback yt-dlp used to recover from it.
        self.assertEqual(
            [item.category for item in warnings],
            ["transport", "extraction", "extraction"],
        )

    def test_a_token_failure_still_outranks_the_transport_it_reports(self) -> None:
        # Job 9's PO token error also mentions a transport failure; the token is the
        # condition worth naming, so its pattern is checked first.
        warnings = classify_warnings(
            "",
            'WARNING: [youtube] [pot] Error fetching PO Token from "bgutil" provider: '
            "connection reset",
        )

        self.assertEqual(warnings[0].category, "po_token")


if __name__ == "__main__":
    unittest.main()
