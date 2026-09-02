from __future__ import annotations

from pathlib import Path
import unittest

from audio_archive.cloud.proxy import YtDlpProxyRunner
from audio_archive.tooling import CommandResult, ToolExecutionError


PROXY = "http://proxy-user:proxy-password@proxy.example:1234"


class RecordingRunner:
    def __init__(self, *, fail: bool = False, output: str = "") -> None:
        self.fail = fail
        self.output = output
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd: Path | None = None) -> CommandResult:
        command = tuple(str(part) for part in argv)
        self.commands.append(command)
        result = CommandResult(
            argv=command,
            returncode=1 if self.fail else 0,
            stdout=self.output,
            stderr=(self.output or "ERROR: simulated failure") if self.fail else self.output,
            started_at_utc="2026-08-26T00:00:00+00:00",
            finished_at_utc="2026-08-26T00:00:01+00:00",
        )
        if self.fail:
            raise ToolExecutionError(result)
        return result


class YtDlpProxyRunnerTests(unittest.TestCase):
    def test_proxy_is_injected_but_redacted_from_returned_command(self) -> None:
        delegate = RecordingRunner()
        runner = YtDlpProxyRunner(delegate, PROXY)

        result = runner.run(("yt-dlp", "--ignore-config", "https://youtu.be/example"))

        self.assertEqual(delegate.commands[0][1:3], ("--proxy", PROXY))
        self.assertEqual(result.argv[1:3], ("--proxy", "<redacted>"))
        self.assertNotIn(PROXY, result.argv)

    def test_proxy_applies_to_search_and_exact_url_yt_dlp_calls(self) -> None:
        delegate = RecordingRunner()
        runner = YtDlpProxyRunner(delegate, PROXY)

        runner.run(("yt-dlp", "--dump-json", "ytsearch5:Artist - Song"))
        runner.run(("/usr/local/bin/yt-dlp", "--write-info-json", "https://youtu.be/example"))

        for command in delegate.commands:
            self.assertEqual(command[1:3], ("--proxy", PROXY))

    def test_non_ytdlp_commands_are_unchanged(self) -> None:
        delegate = RecordingRunner()
        runner = YtDlpProxyRunner(delegate, PROXY)

        result = runner.run(("ffprobe", "-version"))

        self.assertEqual(delegate.commands[0], ("ffprobe", "-version"))
        self.assertEqual(result.argv, ("ffprobe", "-version"))

    def test_failed_ytdlp_command_redacts_proxy_credentials(self) -> None:
        delegate = RecordingRunner(fail=True)
        runner = YtDlpProxyRunner(delegate, PROXY)

        with self.assertRaises(ToolExecutionError) as caught:
            runner.run(("yt-dlp", "--write-info-json", "https://youtu.be/example"))

        self.assertEqual(caught.exception.result.argv[1:3], ("--proxy", "<redacted>"))
        self.assertNotIn(PROXY, caught.exception.result.argv)
        self.assertNotIn("proxy-password", str(caught.exception))

    def test_tool_output_is_redacted_before_it_can_reach_an_ingest_log(self) -> None:
        delegate = RecordingRunner(
            output=f"ERROR: Unable to connect to proxy {PROXY}: connection reset"
        )
        runner = YtDlpProxyRunner(delegate, PROXY)

        result = runner.run(("yt-dlp", "https://youtu.be/example"))

        for stream in (result.stdout, result.stderr):
            self.assertNotIn(PROXY, stream)
            self.assertNotIn("proxy-password", stream)
            self.assertIn("<redacted>", stream)
            self.assertIn("connection reset", stream)

    def test_failed_command_output_and_message_are_redacted(self) -> None:
        delegate = RecordingRunner(fail=True, output=f"ERROR: proxy {PROXY} refused the request")
        runner = YtDlpProxyRunner(delegate, PROXY)

        with self.assertRaises(ToolExecutionError) as caught:
            runner.run(("yt-dlp", "https://youtu.be/example"))

        self.assertNotIn("proxy-password", caught.exception.result.stderr)
        self.assertNotIn("proxy-password", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_other_credential_bearing_urls_in_output_are_redacted(self) -> None:
        delegate = RecordingRunner(output="ERROR: postgresql://archive:hunter2@db.internal/db")
        runner = YtDlpProxyRunner(delegate, PROXY)

        result = runner.run(("yt-dlp", "https://youtu.be/example"))

        self.assertNotIn("hunter2", result.stdout)
        self.assertIn("postgresql://<redacted>@db.internal/db", result.stdout)

    def test_existing_proxy_argument_is_not_duplicated(self) -> None:
        delegate = RecordingRunner()
        runner = YtDlpProxyRunner(delegate, PROXY)

        runner.run(("yt-dlp", "--proxy", "http://explicit.example:8080", "https://youtu.be/example"))

        self.assertEqual(delegate.commands[0].count("--proxy"), 1)


if __name__ == "__main__":
    unittest.main()
