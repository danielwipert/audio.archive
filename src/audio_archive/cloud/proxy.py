from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from ..tooling import CommandResult, CommandRunner, ToolExecutionError


class YtDlpProxyRunner:
    """Route yt-dlp calls through one configured proxy without persisting credentials."""

    def __init__(self, delegate: CommandRunner, proxy_url: str) -> None:
        resolved = proxy_url.strip()
        if not resolved:
            raise ValueError("proxy_url must not be empty")
        self.delegate = delegate
        self.proxy_url = resolved

    def run(self, argv: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        command = tuple(str(part) for part in argv)
        effective = self._with_proxy(command)
        try:
            result = self.delegate.run(effective, cwd=cwd)
        except ToolExecutionError as exc:
            raise ToolExecutionError(_redact_result(exc.result)) from exc
        return _redact_result(result)

    def _with_proxy(self, argv: tuple[str, ...]) -> tuple[str, ...]:
        if not argv or not _is_yt_dlp(argv[0]):
            return argv
        if "--proxy" in argv:
            return argv
        return (argv[0], "--proxy", self.proxy_url, *argv[1:])


def _is_yt_dlp(executable: str) -> bool:
    return Path(executable).name.casefold() in {"yt-dlp", "yt-dlp.exe"}


def _redact_result(result: CommandResult) -> CommandResult:
    return replace(result, argv=_redact_proxy_argv(result.argv))


def _redact_proxy_argv(argv: Sequence[str]) -> tuple[str, ...]:
    redacted: list[str] = []
    hide_next = False
    for part in argv:
        text = str(part)
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        redacted.append(text)
        if text == "--proxy":
            hide_next = True
    return tuple(redacted)
