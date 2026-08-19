from __future__ import annotations

import unittest

from audio_archive.app import QueueController
from audio_archive.cli import _is_loopback_host


class FakeWorker:
    def __init__(self) -> None:
        self._paused = False
        self.run_calls = 0

    @property
    def paused(self) -> bool:
        return self._paused

    def request_pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def recover_startup(self) -> None:
        return None

    def run_until_idle(self) -> tuple[()]:
        self.run_calls += 1
        return ()


class BrowserControlTests(unittest.TestCase):
    def test_loopback_guard_accepts_only_local_hosts(self) -> None:
        self.assertTrue(_is_loopback_host("127.0.0.1"))
        self.assertTrue(_is_loopback_host("::1"))
        self.assertTrue(_is_loopback_host("localhost"))
        self.assertFalse(_is_loopback_host("0.0.0.0"))
        self.assertFalse(_is_loopback_host("192.168.1.25"))
        self.assertFalse(_is_loopback_host("archive.example.com"))

    def test_wake_does_not_override_explicit_pause(self) -> None:
        worker = FakeWorker()
        controller = QueueController(worker)  # type: ignore[arg-type]
        worker.request_pause()

        controller.wake()

        self.assertTrue(worker.paused)
        self.assertFalse(controller._wake.is_set())

    def test_start_explicitly_resumes_and_wakes_queue(self) -> None:
        worker = FakeWorker()
        controller = QueueController(worker)  # type: ignore[arg-type]
        worker.request_pause()

        controller.start()

        self.assertFalse(worker.paused)
        self.assertTrue(controller._wake.is_set())


if __name__ == "__main__":
    unittest.main()
