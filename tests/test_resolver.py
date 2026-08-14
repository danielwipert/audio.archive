import unittest

from audio_archive.resolver import Candidate, decide_resolution, normalize_text, score_candidate


class ResolverTests(unittest.TestCase):
    def test_normalization_is_case_punctuation_and_accent_insensitive(self) -> None:
        self.assertEqual(normalize_text("Beyoncé — Déjà Vu"), "beyonce deja vu")

    def test_strong_official_match_auto_selects(self) -> None:
        candidates = [
            Candidate(
                video_id="AAAAAAAAAAA",
                title="Massive Attack - Teardrop (Official Audio)",
                channel="Massive Attack",
            ),
            Candidate(
                video_id="BBBBBBBBBBB",
                title="Teardrop - Massive Attack cover",
                channel="A Cover Channel",
            ),
        ]
        decision = decide_resolution(
            artist="Massive Attack", title="Teardrop", version=None, candidates=candidates
        )
        self.assertEqual(decision.method, "automatic")
        self.assertEqual(decision.selected.candidate.video_id, "AAAAAAAAAAA")
        self.assertGreaterEqual(decision.margin, 15)

    def test_ambiguous_top_scores_require_review(self) -> None:
        candidates = [
            Candidate("AAAAAAAAAAA", "Portishead - Roads (Official Audio)", "Portishead"),
            Candidate("BBBBBBBBBBB", "Portishead - Roads (Official Video)", "Portishead"),
        ]
        decision = decide_resolution(
            artist="Portishead", title="Roads", version=None, candidates=candidates
        )
        self.assertEqual(decision.method, "needs_review")
        self.assertIsNone(decision.selected)

    def test_unrequested_live_version_is_disqualified(self) -> None:
        result = score_candidate(
            artist="Radiohead",
            title="Everything in Its Right Place",
            version=None,
            candidate=Candidate(
                "AAAAAAAAAAA",
                "Radiohead - Everything in Its Right Place (Live)",
                "Radiohead",
            ),
        )
        self.assertTrue(result.disqualified)
        self.assertIn("unrequested version term: live", result.warnings)

    def test_requested_live_version_is_not_disqualified(self) -> None:
        result = score_candidate(
            artist="Radiohead",
            title="Everything in Its Right Place",
            version="live",
            candidate=Candidate(
                "AAAAAAAAAAA",
                "Radiohead - Everything in Its Right Place (Live)",
                "Radiohead",
            ),
        )
        self.assertFalse(result.disqualified)
        self.assertGreaterEqual(result.score, 90)


if __name__ == "__main__":
    unittest.main()

