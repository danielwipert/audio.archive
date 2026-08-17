from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import os
import shutil
import subprocess
from typing import Protocol, Sequence


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    started_at_utc: str
    finished_at_utc: str


class ToolExecutionError(RuntimeError):
    def __init__(self, result: CommandResult):
        self.result = result
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        super().__init__(f"Command failed with exit code {result.returncode}: {detail[-1000:]}")


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, cwd: Path | None = None) -> CommandResult: ...


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def resolve_tool(configured: str, tools_directory: Path) -> str:
    raw = Path(configured)
    if raw.is_absolute():
        if not raw.is_file():
            raise FileNotFoundError(f"Configured tool does not exist: {raw}")
        return str(raw)

    names = [configured]
    if os.name == "nt" and not raw.suffix:
        names.extend(f"{configured}{suffix}" for suffix in (".exe", ".cmd", ".bat"))
    for name in names:
        bundled = tools_directory / name
        if bundled.is_file():
            return str(bundled.resolve())
    located = shutil.which(configured)
    if located:
        return located
    raise FileNotFoundError(
        f"Required tool '{configured}' was not found in {tools_directory} or PATH"
    )


class SubprocessRunner:
    """Runs controlled argv arrays without a shell or interactive standard input."""

    def run(self, argv: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        if not argv:
            raise ValueError("Command argv cannot be empty")
        started = utc_now()
        completed = subprocess.run(
            [str(part) for part in argv],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
            env={**os.environ, "NO_COLOR": "1"},
        )
        result = CommandResult(
            argv=tuple(str(part) for part in argv),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            started_at_utc=started,
            finished_at_utc=utc_now(),
        )
        if result.returncode:
            raise ToolExecutionError(result)
        return result


def read_tool_version(runner: CommandRunner, tool: str, *args: str) -> str:
    result = runner.run((tool, *args))
    output = result.stdout.strip() or result.stderr.strip()
    return output.splitlines()[0].strip() if output else "unknown"

