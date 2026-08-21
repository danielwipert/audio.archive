import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from audio_archive.db import ArchiveDatabase
from audio_archive.models import JobRequest, JobState


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.database = ArchiveDatabase(Path(self.temporary.name) / "archive.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_manual_search_job_starts_pending(self) -> None:
        job_id = self.database.create_job(JobRequest(artist="Portishead", title="Roads"))
        self.assertEqual(self.database.get_job(job_id)["state"], JobState.PENDING.value)

    def test_exact_url_job_starts_ready(self) -> None:
        job_id = self.database.create_job(
            JobRequest(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", origin="url")
        )
        job = self.database.get_job(job_id)
        self.assertEqual(job["state"], JobState.READY.value)
        self.assertEqual(job["source_extractor"], "youtube")
        self.assertEqual(job["source_id"], "dQw4w9WgXcQ")
        self.assertEqual(job["resolution_method"], "exact_url")

    def test_transition_is_audited_and_invalid_transition_fails(self) -> None:
        job_id = self.database.create_job(JobRequest(artist="Radiohead", title="Nude"))
        self.database.transition_job(job_id, JobState.RESOLVING)
        with self.assertRaises(ValueError):
            self.database.transition_job(job_id, JobState.COMPLETED)

    def test_startup_marks_active_jobs_interrupted(self) -> None:
        job_id = self.database.create_job(JobRequest(artist="Björk", title="Jóga"))
        self.database.transition_job(job_id, JobState.RESOLVING)
        self.assertEqual(self.database.interrupt_active_jobs(), 1)
        self.assertEqual(self.database.get_job(job_id)["state"], JobState.INTERRUPTED.value)

    def test_csv_provenance_is_available_with_job(self) -> None:
        import_id = self.database.create_csv_import(
            filename="songs.csv",
            file_sha256="a" * 64,
            accepted_rows=1,
            rejected_rows=0,
            duplicate_rows=0,
        )
        job_id = self.database.create_job(
            JobRequest(
                artist="Portishead",
                title="Roads",
                origin="csv",
                import_id=import_id,
                import_row=2,
            )
        )
        job = self.database.get_job(job_id)
        self.assertEqual(job["import_filename"], "songs.csv")
        self.assertEqual(job["import_file_sha256"], "a" * 64)

    def test_worker_claim_is_exclusive_and_releasable(self) -> None:
        job_id = self.database.create_job(
            JobRequest(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", origin="url")
        )
        self.assertEqual(self.database.claim_next_runnable_job("worker-one"), job_id)
        self.assertIsNone(self.database.claim_next_runnable_job("worker-two"))
        self.assertFalse(self.database.release_worker_claim(job_id, "wrong-token"))
        self.assertTrue(self.database.release_worker_claim(job_id, "worker-one"))
        self.assertEqual(self.database.claim_next_runnable_job("worker-two"), job_id)

    def test_retry_requeues_source_and_increments_counter(self) -> None:
        job_id = self.database.create_job(
            JobRequest(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", origin="url")
        )
        self.database.fail_job(job_id, stage="ready", summary="temporary failure")

        target = self.database.retry_job(job_id)

        job = self.database.get_job(job_id)
        self.assertEqual(target, JobState.READY)
        self.assertEqual(job["state"], JobState.READY.value)
        self.assertEqual(job["retry_count"], 1)
        self.assertIsNone(job["error_summary"])

    def test_schema_one_database_migrates_worker_claim_table(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as connection:
            with connection:
                connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY)")
                connection.execute("PRAGMA user_version = 1")

        legacy = ArchiveDatabase(legacy_path)
        legacy.initialize()

        with legacy.connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'worker_claims'"
            ).fetchone()
        self.assertEqual(version, 2)
        self.assertIsNotNone(table)


if __name__ == "__main__":
    unittest.main()
