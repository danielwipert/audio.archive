from __future__ import annotations

import sys

import pytest

from audio_archive.tooling import SubprocessRunner, ToolExecutionError


def test_subprocess_runner_stops_timed_out_command() -> None:
    runner = SubprocessRunner(timeout_seconds=0.05)

    with pytest.raises(ToolExecutionError) as error:
        runner.run((sys.executable, "-c", "import time; time.sleep(1)"))

    assert error.value.result.returncode == 124
    assert "timed out after 0.05 seconds" in error.value.result.stderr
