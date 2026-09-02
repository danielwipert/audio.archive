from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

from .config import AppConfig
from .tooling import MINIMUM_DENO_VERSION, CommandRunner, read_tool_version, resolve_tool


@dataclass(frozen=True)
class Diagnostic:
    name: str
    ok: bool
    required: bool
    version: str | None
    path: str | None
    message: str


@dataclass(frozen=True)
class DoctorReport:
    ready: bool
    diagnostics: tuple[Diagnostic, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", value)
    return tuple(map(int, match.groups())) if match else None


def _command_diagnostic(
    *,
    name: str,
    configured: str,
    tools_directory: Path,
    runner: CommandRunner,
    version_args: tuple[str, ...],
    validator: Callable[[str], tuple[bool, str]],
) -> Diagnostic:
    try:
        path = resolve_tool(configured, tools_directory)
        version = read_tool_version(runner, path, *version_args)
        ok, message = validator(version)
        return Diagnostic(name, ok, True, version, path, message)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return Diagnostic(name, False, True, None, None, str(exc))


def _present(version: str) -> tuple[bool, str]:
    ok = bool(version and version != "unknown")
    return ok, "available" if ok else "version unavailable"


def _yt_dlp_validator(
    distribution_version: Callable[[str], str],
) -> Callable[[str], tuple[bool, str]]:
    """Compare the resolved executable against the yt-dlp version this project installs.

    The pin lives in `pyproject.toml`, so it is read from the installed distribution
    rather than repeated here, where it went stale behind a dependency update before.
    """

    try:
        expected = _version_tuple(distribution_version("yt-dlp"))
    except metadata.PackageNotFoundError:
        expected = None

    def validate(version: str) -> tuple[bool, str]:
        if expected is None:
            return False, "the pinned yt-dlp distribution is not installed"
        if _version_tuple(version) == expected:
            return True, "matches the project pin"
        return False, f"expected pinned version {'.'.join(map(str, expected))}"

    return validate


def _deno_version(version: str) -> tuple[bool, str]:
    parsed = _version_tuple(version)
    if parsed and parsed >= MINIMUM_DENO_VERSION:
        return True, "meets the minimum supported version"
    minimum = ".".join(map(str, MINIMUM_DENO_VERSION))
    return False, f"Deno {minimum} or newer is required"


def run_doctor(
    config: AppConfig,
    runner: CommandRunner,
    *,
    distribution_version: Callable[[str], str] = metadata.version,
) -> DoctorReport:
    diagnostics: list[Diagnostic] = []
    python_ok = sys.version_info >= (3, 11)
    diagnostics.append(
        Diagnostic(
            "Python",
            python_ok,
            True,
            ".".join(map(str, sys.version_info[:3])),
            sys.executable,
            "supported" if python_ok else "Python 3.11 or newer is required",
        )
    )
    diagnostics.extend(
        (
            _command_diagnostic(
                name="yt-dlp",
                configured=config.yt_dlp,
                tools_directory=config.tools_directory,
                runner=runner,
                version_args=("--version",),
                validator=_yt_dlp_validator(distribution_version),
            ),
            _command_diagnostic(
                name="Deno",
                configured=config.deno,
                tools_directory=config.tools_directory,
                runner=runner,
                version_args=("--version",),
                validator=_deno_version,
            ),
            _command_diagnostic(
                name="FFmpeg",
                configured=config.ffmpeg,
                tools_directory=config.tools_directory,
                runner=runner,
                version_args=("-version",),
                validator=_present,
            ),
            _command_diagnostic(
                name="FFprobe",
                configured=config.ffprobe,
                tools_directory=config.tools_directory,
                runner=runner,
                version_args=("-version",),
                validator=_present,
            ),
        )
    )
    try:
        ejs_version = distribution_version("yt-dlp-ejs")
        diagnostics.append(
            Diagnostic(
                "yt-dlp EJS",
                True,
                True,
                ejs_version,
                None,
                "matching challenge components are installed",
            )
        )
    except metadata.PackageNotFoundError:
        diagnostics.append(
            Diagnostic(
                "yt-dlp EJS",
                False,
                True,
                None,
                None,
                "yt-dlp-ejs is missing; reinstall the project dependencies",
            )
        )

    loopback_ok = config.host == "127.0.0.1"
    diagnostics.append(
        Diagnostic(
            "Loopback binding",
            loopback_ok,
            True,
            None,
            config.host,
            "local-only" if loopback_ok else "host must be 127.0.0.1",
        )
    )
    archive_parent = config.archive_root.parent
    writable = (
        archive_parent.exists()
        and archive_parent.is_dir()
        and os.access(archive_parent, os.W_OK)
    )
    diagnostics.append(
        Diagnostic(
            "Archive location",
            writable,
            True,
            None,
            str(config.archive_root),
            "parent directory is writable" if writable else "archive parent is not writable",
        )
    )
    return DoctorReport(
        ready=all(item.ok for item in diagnostics if item.required),
        diagnostics=tuple(diagnostics),
    )


def format_report(report: DoctorReport) -> str:
    lines = []
    for item in report.diagnostics:
        status = "OK" if item.ok else "FAIL"
        detail = item.version or item.path or item.message
        lines.append(f"[{status}] {item.name}: {detail} — {item.message}")
    lines.append("READY" if report.ready else "NOT READY")
    return "\n".join(lines)
