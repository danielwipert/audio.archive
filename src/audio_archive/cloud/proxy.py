from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from ..tooling import CommandResult, CommandRunner, ToolExecutionError

REDACTED = "<redacted>"
CREDENTIAL_URL_PATTERN = re.compile(r"(?P<scheme>[A-Za-z][\w+.-]*)://[^\s/@]+:[^\s/@]+@")


class YtDlpProxyRunner:
    """Route yt-dlp calls through one configured proxy without persisting credentials."""

    def __init__(self, delegate: CommandRunner, proxy_url: str) -> None:
        resolved = proxy_url.strip()
        if not resolved:
            raise ValueError("proxy_url must not be empty")
        self.delegate = delegate
        self.proxy_url = resolved
        self._secrets = _proxy_secrets(resolved)

    def run(self, argv: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        command = tuple(str(part) for part in argv)
        effective = self._with_proxy(command)
        try:
            result = self.delegate.run(effective, cwd=cwd)
        except ToolExecutionError as exc:
            # The original exception is not chained: its message carries the unredacted
            # tool output, which a traceback would return to logs.
            raise ToolExecutionError(self._redact_result(exc.result)) from None
        return self._redact_result(result)

    def _with_proxy(self, argv: tuple[str, ...]) -> tuple[str, ...]:
        if not argv or not _is_yt_dlp(argv[0]):
            return argv
        if "--proxy" in argv:
            return argv
        return (argv[0], "--proxy", self.proxy_url, *argv[1:])

    def _redact_result(self, result: CommandResult) -> CommandResult:
        """Remove proxy credentials from everything a job can durably record.

        Ingest logs keep the command output verbatim and are published as a downloadable
        artifact, so the output is redacted here rather than at each recording site.
        """

        return replace(
            result,
            argv=_redact_proxy_argv(result.argv),
            stdout=self._redact_text(result.stdout),
            stderr=self._redact_text(result.stderr),
        )

    def _redact_text(self, text: str) -> str:
        if not text:
            return text
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return CREDENTIAL_URL_PATTERN.sub(rf"\g<scheme>://{REDACTED}@", text)


def _proxy_secrets(proxy_url: str) -> tuple[str, ...]:
    """Return the literal strings that must never survive in recorded output."""

    parts = urlsplit(proxy_url)
    candidates = {proxy_url, parts.password or ""}
    if parts.username and parts.password:
        candidates.add(f"{parts.username}:{parts.password}")
    # Longest first so a password is not partially replaced inside the full URL.
    return tuple(sorted((item for item in candidates if item), key=len, reverse=True))


def _is_yt_dlp(executable: str) -> bool:
    return Path(executable).name.casefold() in {"yt-dlp", "yt-dlp.exe"}


def _redact_proxy_argv(argv: Sequence[str]) -> tuple[str, ...]:
    redacted: list[str] = []
    hide_next = False
    for part in argv:
        text = str(part)
        if hide_next:
            redacted.append(REDACTED)
            hide_next = False
            continue
        redacted.append(text)
        if text == "--proxy":
            hide_next = True
    return tuple(redacted)
