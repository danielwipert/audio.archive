from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_ableton import make_config

from audio_archive.db import ArchiveDatabase
from audio_archive.models import JobRequest, JobState
from audio_archive.source_resolution import (
    approve_candidate,
    claim_next_queue_job,
    list_resolution_candidates,
    mark_not_found,
    replace_source_url,
    resolve_pending_job,
)
from audio_archive.tooling import CommandResult


class JsonRunner:
    def __init__(self, payloads: list[dict[str, object]]):
        self.payloads = payloads
        self.argv: tuple[str, ...] | None = None

    def run(self, argv, *, cwd=None):
        self.argv = tuple(str(part) for part in argv)
        return CommandResult(
            argv=self.argv,
            returncode=0,
            stdout="\n".join(json.dumps(payload) for payload in self.payloads),
            stderr="",
            started_at_utc="2026-08-19T18:00:00+00:00",
            finished_at_utc="2026-08-19T18:00:01+00:00",
        )


class SourceResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.config = make_config(Path(self.temporary.name))
        (self.config.tools_directory / "yt-dlp").write_text("test tool", encoding="utf-8")
        self.database = ArchiveDatabase(self.config.database_path)
        self.database.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _job(self, *, artist: str = "Massive Attack", title: str = "Teardrop") -> int:
        return self.database.create_job(JobRequest(artist=artist, title=title, profile="archive"))

    def test_strong_search_match_is_persisted_and_pinned_automatically(self) -> None:
        job_id = self._job()
        runner = JsonRunner(
            [
                {
                    "id": "AAAAAAAAAAA",
                    "title": "Massive Attack - Teardrop (Official Audio)",
                    "channel": "Massive Attack",
                    "duration": 330,
                    "thumbnail": "https://example.test/a.jpg",
                },
                {
                    "id": "BBBBBBBBBBB",
                    "title": "Massive Attack - Teardrop cover",
                    "channel": "Cover Channel",
                    "duration": 329,
                },
            ]
        )

        result = resolve_pending_job(self.database, self.config, runner, job_id)

        job = self.database.get_job(job_id)
        candidates = list_resolution_candidates(self.database, job_id)
        self.assertEqual(result.state, JobState.READY)
        self.assertEqual(job["source_id"], "AAAAAAAAAAA")
        self.assertEqual(job["resolution_method"], "automatic")
        self.assertEqual([row["position"] for row in candidates], [1, 2])
        self.assertEqual(candidates[0]["video_id"], "AAAAAAAAAAA")
        self.assertGreaterEqual(candidates[0]["score"], 90)
        self.assertIn("--ignore-config", runner.argv)
        self.assertIn("--flat-playlist", runner.argv)
        self.assertIn("--skip-download", runner.argv)
        self.assertEqual(runner.argv[runner.argv.index("--socket-timeout") + 1], "30")
        self.assertEqual(runner.argv[runner.argv.index("--retries") + 1], "3")
        self.assertEqual(runner.argv[runner.argv.index("--extractor-retries") + 1], "2")
        self.assertTrue(runner.argv[-1].startswith("ytsearch5:"))

    def test_ambiguous_search_waits_for_manual_review(self) -> None:
        job_id = self.database.create_job(
            JobRequest(artist="Portishead", title="Roads", profile="archive")
        )
        runner = JsonRunner(
            [
                {
                    "id": "AAAAAAAAAAA",
                    "title": "Portishead - Roads (Official Audio)",
                    "channel": "Portishead",
                },
                {
                    "id": "BBBBBBBBBBB",
                    "title": "Portishead - Roads (Official Video)",
                    "channel": "Portishead",
                },
            ]
        )

        result = resolve_pending_job(self.database, self.config, runner, job_id)

        job = self.database.get_job(job_id)
        self.assertEqual(result.state, JobState.NEEDS_REVIEW)
        self.assertEqual(job["state"], JobState.NEEDS_REVIEW.value)
        self.assertEqual(job["resolution_method"], "needs_review")
        self.assertIsNone(job["source_id"])
        self.assertEqual(len(list_resolution_candidates(self.database, job_id)), 2)

    def test_user_can_approve_one_recorded_candidate(self) -> None:
        job_id = self.database.create_job(
            JobRequest(artist="Portishead", title="Roads", profile="archive")
        )
        runner = JsonRunner(
            [
                {
                    "id": "AAAAAAAAAAA",
                    "title": "Portishead - Roads (Official Audio)",
                    "channel": "Portishead",
                },
                {
                    "id": "BBBBBBBBBBB",
                    "title": "Portishead - Roads (Official Video)",
                    "channel": "Portishead",
                },
            ]
        )
        resolve_pending_job(self.database, self.config, runner, job_id)

        approve_candidate(self.database, job_id, "BBBBBBBBBBB")

        job = self.database.get_job(job_id)
        self.assertEqual(job["state"], JobState.READY.value)
        self.assertEqual(job["source_id"], "BBBBBBBBBBB")
        self.assertEqual(job["resolution_method"], "manual_selection")

    def test_user_can_supply_replacement_url_or_mark_not_found(self) -> None:
        first = self.database.create_job(JobRequest(artist="Portishead", title="Roads"))
        second = self.database.create_job(JobRequest(artist="Portishead", title="Roads"))
        payloads = [
            {
                "id": "AAAAAAAAAAA",
                "title": "Portishead - Roads (Official Audio)",
                "channel": "Portishead",
            },
            {
                "id": "BBBBBBBBBBB",
                "title": "Portishead - Roads (Official Video)",
                "channel": "Portishead",
            },
        ]
        resolve_pending_job(self.database, self.config, JsonRunner(payloads), first)
        resolve_pending_job(self.database, self.config, JsonRunner(payloads), second)

        replace_source_url(
            self.database,
            first,
            "https://youtu.be/CCCCCCCCCCC",
        )
        mark_not_found(self.database, second)

        replaced = self.database.get_job(first)
        missing = self.database.get_job(second)
        self.assertEqual(replaced["state"], JobState.READY.value)
        self.assertEqual(replaced["source_id"], "CCCCCCCCCCC")
        self.assertEqual(replaced["resolution_method"], "manual_url")
        self.assertEqual(missing["state"], JobState.NOT_FOUND.value)
        self.assertEqual(missing["resolution_method"], "manual_not_found")

    def test_empty_search_marks_job_not_found(self) -> None:
        job_id = self._job()

        result = resolve_pending_job(self.database, self.config, JsonRunner([]), job_id)

        job = self.database.get_job(job_id)
        self.assertEqual(result.state, JobState.NOT_FOUND)
        self.assertEqual(job["state"], JobState.NOT_FOUND.value)
        self.assertEqual(job["resolution_method"], "not_found")
        self.assertEqual(job["progress_percent"], 100)

    def test_worker_claim_can_select_pending_resolution_work(self) -> None:
        pending = self._job()
        ready = self.database.create_job(
            JobRequest(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", origin="url")
        )

        claimed = claim_next_queue_job(self.database, "worker-test")

        self.assertEqual(claimed, pending)
        self.assertTrue(self.database.release_worker_claim(pending, "worker-test"))
        self.assertEqual(claim_next_queue_job(self.database, "worker-test-2"), pending)
        self.assertNotEqual(pending, ready)


if __name__ == "__main__":
    unittest.main()
