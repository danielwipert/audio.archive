from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_ableton import URL, make_config

from audio_archive.cli import _init_command
from audio_archive.db import ArchiveDatabase
from audio_archive.models import JobRequest, JobState
from audio_archive.worker import SequentialWorker, _claimed_process_is_alive


class NoopRunner:
    def run(self, argv, *, cwd=None):
        raise AssertionError("No external command should run in worker orchestration tests")


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.config = make_config(Path(self.temporary.name))
        self.database = ArchiveDatabase(self.config.database_path)
        self.database.initialize()
        self.worker = SequentialWorker(self.database, self.config, NoopRunner())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _url_job(self, *, profile: str = "archive") -> int:
        return self.database.create_job(JobRequest(url=URL, origin="url", profile=profile))

    def _complete_fake_acquisition(self, job_id: int) -> None:
        self.database.transition_job(job_id, JobState.DOWNLOADING)
        self.database.transition_job(job_id, JobState.VERIFYING_MASTER)
        self.database.transition_job(job_id, JobState.COMPLETED)

    def test_queue_continues_after_recording_one_job_failure(self) -> None:
        first = self._url_job()
        second = self._url_job()

        def acquire(database, config, runner, job_id):
            if job_id == first:
                raise RuntimeError("fixture acquisition failed")
            self._complete_fake_acquisition(job_id)

        with patch("audio_archive.worker.acquire_ready_job", side_effect=acquire):
            results = self.worker.run_until_idle()

        self.assertEqual([result.job_id for result in results], [first, second])
        self.assertEqual(self.database.get_job(first)["state"], JobState.FAILED.value)
        self.assertEqual(self.database.get_job(second)["state"], JobState.COMPLETED.value)
        self.assertIn("fixture acquisition failed", results[0].error)

    def test_pause_request_takes_effect_after_current_job(self) -> None:
        first = self._url_job()
        second = self._url_job()

        def acquire(database, config, runner, job_id):
            self._complete_fake_acquisition(job_id)
            self.worker.request_pause()

        with patch("audio_archive.worker.acquire_ready_job", side_effect=acquire):
            results = self.worker.run_until_idle()
            self.assertEqual([result.job_id for result in results], [first])
            self.assertEqual(self.database.get_job(second)["state"], JobState.READY.value)
            self.worker.resume()
            resumed = self.worker.run_until_idle()

        self.assertEqual([result.job_id for result in resumed], [second])

    def test_complete_profile_runs_both_derivatives_in_order(self) -> None:
        self._url_job(profile="complete")
        stages: list[str] = []

        def acquire(database, config, runner, selected_job_id):
            database.transition_job(selected_job_id, JobState.DOWNLOADING)
            database.transition_job(selected_job_id, JobState.VERIFYING_MASTER)
            database.transition_job(selected_job_id, JobState.CONVERTING)

        def ableton(database, config, runner, selected_job_id):
            stages.append("ableton")

        def listening(database, config, runner, selected_job_id):
            stages.append("listening")
            database.transition_job(selected_job_id, JobState.VERIFYING_OUTPUT)
            database.transition_job(selected_job_id, JobState.COMPLETED)

        with (
            patch("audio_archive.worker.acquire_ready_job", side_effect=acquire),
            patch("audio_archive.worker.create_ableton_for_job", side_effect=ableton),
            patch("audio_archive.worker.create_listening_for_job", side_effect=listening),
        ):
            result = self.worker.run_next()

        self.assertEqual(stages, ["ableton", "listening"])
        self.assertEqual(result.state, JobState.COMPLETED)

    def test_startup_requeues_interrupted_job_and_clears_stale_claim(self) -> None:
        job_id = self._url_job()
        self.assertEqual(self.database.claim_next_runnable_job("stale-worker"), job_id)
        self.database.transition_job(job_id, JobState.DOWNLOADING)

        recovery = self.worker.recover_startup()

        self.assertEqual(recovery.interrupted_jobs, 1)
        self.assertEqual(recovery.stale_claims, 1)
        self.assertEqual(recovery.requeued_jobs, 1)
        self.assertEqual(self.database.get_job(job_id)["state"], JobState.READY.value)
        events = self.database.list_job_events(job_id)
        self.assertEqual(events[-1]["event_type"], "recovery")

    def test_startup_refuses_to_interrupt_a_live_worker(self) -> None:
        job_id = self._url_job()
        token = f"{os.getpid()}:live-worker"
        self.assertEqual(self.database.claim_next_runnable_job(token), job_id)
        self.database.transition_job(job_id, JobState.DOWNLOADING)

        with self.assertRaisesRegex(ValueError, "Another queue worker"):
            self.worker.recover_startup()

        self.assertEqual(self.database.get_job(job_id)["state"], JobState.DOWNLOADING.value)
        self.assertEqual(len(self.database.list_worker_claims()), 1)

    def test_windows_liveness_probe_never_calls_os_kill(self) -> None:
        with (
            patch("audio_archive.worker.os.name", "nt"),
            patch(
                "audio_archive.worker._windows_process_is_alive", return_value=True
            ) as windows_probe,
            patch("audio_archive.worker.os.kill") as kill,
        ):
            alive = _claimed_process_is_alive("123:worker")

        self.assertTrue(alive)
        windows_probe.assert_called_once_with(123)
        kill.assert_not_called()

    def test_init_refuses_to_interrupt_a_live_worker(self) -> None:
        job_id = self._url_job()
        token = f"{os.getpid()}:live-worker"
        self.assertEqual(self.database.claim_next_runnable_job(token), job_id)
        self.database.transition_job(job_id, JobState.DOWNLOADING)

        with patch("audio_archive.cli._database", return_value=(self.database, self.config)):
            with self.assertRaisesRegex(ValueError, "Another queue worker"):
                _init_command()

        self.assertEqual(self.database.get_job(job_id)["state"], JobState.DOWNLOADING.value)
        self.assertEqual(len(self.database.list_worker_claims()), 1)

    def test_pending_resolution_job_does_not_block_ready_job(self) -> None:
        pending = self.database.create_job(JobRequest(artist="Portishead", title="Roads"))
        ready = self._url_job()

        with patch(
            "audio_archive.worker.acquire_ready_job",
            side_effect=lambda database, config, runner, job_id: self._complete_fake_acquisition(
                job_id
            ),
        ):
            result = self.worker.run_next()

        self.assertEqual(result.job_id, ready)
        self.assertEqual(self.database.get_job(pending)["state"], JobState.PENDING.value)


if __name__ == "__main__":
    unittest.main()
