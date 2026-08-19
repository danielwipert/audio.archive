from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from test_ableton import URL, make_config

from audio_archive.app import create_app
from audio_archive.models import JobRequest, JobState
from audio_archive.source_resolution import resolve_pending_job
from audio_archive.tooling import CommandResult


class NoopRunner:
    def run(self, argv, *, cwd=None):
        raise AssertionError("No external command should run in browser route tests")


class JsonRunner:
    def __init__(self, payloads: list[dict[str, object]]):
        self.payloads = payloads

    def run(self, argv, *, cwd=None):
        return CommandResult(
            argv=tuple(str(part) for part in argv),
            returncode=0,
            stdout="\n".join(json.dumps(payload) for payload in self.payloads),
            stderr="",
            started_at_utc="2026-08-19T18:00:00+00:00",
            finished_at_utc="2026-08-19T18:00:01+00:00",
        )


class BrowserAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.config = make_config(Path(self.temporary.name))
        (self.config.tools_directory / "yt-dlp").write_text("test tool", encoding="utf-8")
        self.app = create_app(self.config, NoopRunner(), start_worker_thread=False)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.database = self.app.state.database

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary.cleanup()

    def test_home_page_loads_single_screen_application(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Audio Archive", response.text)
        self.assertIn("Add audio", response.text)
        self.assertIn("Import CSV", response.text)
        self.assertIn("Needs review", response.text)
        self.assertIn('/static/app.js', response.text)

    def test_manual_artist_title_submission_creates_pending_job(self) -> None:
        response = self.client.post(
            "/api/jobs",
            data={
                "artist": "Massive Attack",
                "title": "Teardrop",
                "profile": "ableton",
                "start_queue": "false",
            },
        )

        self.assertEqual(response.status_code, 200)
        job_id = response.json()["job_id"]
        job = self.database.get_job(job_id)
        self.assertEqual(job["state"], JobState.PENDING.value)
        self.assertEqual(job["requested_artist"], "Massive Attack")
        self.assertEqual(job["requested_title"], "Teardrop")
        self.assertEqual(job["profile"], "ableton")

        status = self.client.get("/api/status").json()
        self.assertEqual(status["jobs"][0]["id"], job_id)
        self.assertEqual(status["jobs"][0]["state"], JobState.PENDING.value)

    def test_exact_url_submission_bypasses_resolution(self) -> None:
        response = self.client.post(
            "/api/jobs",
            data={"url": URL, "profile": "archive", "start_queue": "false"},
        )

        self.assertEqual(response.status_code, 200)
        job = self.database.get_job(response.json()["job_id"])
        self.assertEqual(job["state"], JobState.READY.value)
        self.assertEqual(job["resolution_method"], "exact_url")
        self.assertEqual(job["source_url"], URL)

    def test_invalid_manual_submission_returns_useful_error(self) -> None:
        response = self.client.post(
            "/api/jobs",
            data={"artist": "Massive Attack", "start_queue": "false"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("artist and title are required", response.json()["detail"])

    def test_csv_preview_and_import_preserve_valid_rows_and_provenance(self) -> None:
        csv_bytes = (
            "artist,title,version,url,profile\n"
            "Massive Attack,Teardrop,,,\n"
            "Portishead,Roads,,,archive\n"
            "Bad row,,,,\n"
            "Massive Attack,Teardrop,,,\n"
        ).encode("utf-8")

        preview_response = self.client.post(
            "/api/csv/preview",
            files={"file": ("songs.csv", csv_bytes, "text/csv")},
        )

        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.json()
        self.assertEqual(preview["filename"], "songs.csv")
        self.assertEqual(len(preview["accepted"]), 2)
        self.assertEqual(len(preview["rejected"]), 1)
        self.assertEqual(preview["duplicate_rows"], [5])

        import_response = self.client.post(
            f"/api/csv/import/{preview['token']}",
            data={"start_queue": "false"},
        )
        self.assertEqual(import_response.status_code, 200)
        created = import_response.json()["job_ids"]
        self.assertEqual(len(created), 2)

        first = self.database.get_job(created[0])
        second = self.database.get_job(created[1])
        self.assertEqual(first["import_filename"], "songs.csv")
        self.assertEqual(first["import_row"], 2)
        self.assertEqual(second["import_row"], 3)
        self.assertEqual(second["profile"], "archive")

    def test_csv_preview_token_is_single_use(self) -> None:
        preview = self.client.post(
            "/api/csv/preview",
            files={"file": ("songs.csv", b"artist,title\nPortishead,Roads\n", "text/csv")},
        ).json()

        first = self.client.post(
            f"/api/csv/import/{preview['token']}",
            data={"start_queue": "false"},
        )
        second = self.client.post(
            f"/api/csv/import/{preview['token']}",
            data={"start_queue": "false"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertIn("no longer available", second.json()["detail"])

    def test_review_candidates_are_exposed_and_can_be_approved(self) -> None:
        job_id = self.database.create_job(
            JobRequest(artist="Portishead", title="Roads", profile="archive")
        )
        resolve_pending_job(
            self.database,
            self.config,
            JsonRunner(
                [
                    {
                        "id": "AAAAAAAAAAA",
                        "title": "Portishead - Roads (Official Audio)",
                        "channel": "Portishead",
                        "thumbnail": "https://example.test/a.jpg",
                    },
                    {
                        "id": "BBBBBBBBBBB",
                        "title": "Portishead - Roads (Official Video)",
                        "channel": "Portishead",
                    },
                ]
            ),
            job_id,
        )

        status = self.client.get("/api/status").json()
        job_payload = status["jobs"][0]
        self.assertEqual(job_payload["state"], JobState.NEEDS_REVIEW.value)
        self.assertEqual(len(job_payload["candidates"]), 2)
        self.assertEqual(job_payload["candidates"][0]["video_id"], "AAAAAAAAAAA")
        self.assertIsInstance(job_payload["candidates"][0]["reasons"], list)

        approved = self.client.post(f"/api/jobs/{job_id}/approve/BBBBBBBBBBB")
        self.assertEqual(approved.status_code, 200)
        job = self.database.get_job(job_id)
        self.assertEqual(job["state"], JobState.READY.value)
        self.assertEqual(job["source_id"], "BBBBBBBBBBB")
        self.assertEqual(job["resolution_method"], "manual_selection")

    def test_review_replacement_url_and_not_found_actions(self) -> None:
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
        replace_job = self.database.create_job(JobRequest(artist="Portishead", title="Roads"))
        missing_job = self.database.create_job(JobRequest(artist="Portishead", title="Roads"))
        resolve_pending_job(self.database, self.config, JsonRunner(payloads), replace_job)
        resolve_pending_job(self.database, self.config, JsonRunner(payloads), missing_job)

        replaced = self.client.post(
            f"/api/jobs/{replace_job}/replace-source",
            data={"url": "https://youtu.be/CCCCCCCCCCC"},
        )
        missing = self.client.post(f"/api/jobs/{missing_job}/not-found")

        self.assertEqual(replaced.status_code, 200)
        self.assertEqual(self.database.get_job(replace_job)["source_id"], "CCCCCCCCCCC")
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(self.database.get_job(missing_job)["state"], JobState.NOT_FOUND.value)

    def test_queue_pause_and_resume_use_shared_worker_state(self) -> None:
        paused = self.client.post("/api/queue/pause").json()
        self.assertTrue(paused["paused"])

        resumed = self.client.post("/api/queue/resume").json()
        self.assertFalse(resumed["paused"])


if __name__ == "__main__":
    unittest.main()
