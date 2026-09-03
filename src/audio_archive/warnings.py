from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class AcquisitionWarning:
    category: str
    message: str
    quality_affecting: bool


QUALITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("javascript_runtime", re.compile(r"javascript runtime|js runtime", re.I)),
    ("challenge", re.compile(r"challenge|signature extraction|nsig|n parameter", re.I)),
    ("po_token", re.compile(r"proof.of.origin|po token|po_token", re.I)),
    ("authentication", re.compile(r"sign in|authentication|cookies?", re.I)),
    ("format", re.compile(r"requested format|format.+unavailable|missing formats?", re.I)),
    ("region", re.compile(r"geo.?restrict|not available in your country|region", re.I)),
    ("throttling", re.compile(r"throttl|rate.?limit", re.I)),
    (
        "transport",
        re.compile(
            r"wrong version number|\bssl\b|connection reset|connection aborted"
            r"|tunnel connection failed|timed out|read timeout",
            re.I,
        ),
    ),
    (
        "extraction",
        re.compile(
            r"unable to extract|incomplete data|re.?fetching using api|falling back",
            re.I,
        ),
    ),
)


SOURCE_ACCESS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SourceAccessRateLimited", re.compile(r"http error 429|too many requests", re.I)),
    ("SourceAccessBotCheck", re.compile(r"sign in to confirm|confirm you.{0,3}re not a bot", re.I)),
    ("SourceAccessForbidden", re.compile(r"http error 403|access to this content", re.I)),
    ("SourceAccessTokenFailure", re.compile(r"po.?token|proof.of.origin|botguard", re.I)),
    (
        "SourceUnavailable",
        re.compile(
            r"video unavailable|private video|removed by the uploader"
            r"|not available in your country|geo.?restrict|members.only",
            re.I,
        ),
    ),
)


def classify_source_access_failure(stdout: str, stderr: str) -> str | None:
    """Name the YouTube access restriction that stopped a tool run, when there is one.

    Patterns are ordered by how the failure should be acted on rather than by how the
    output reads: a rate-limited egress path is reported ahead of the bot challenge that
    usually follows it, because the egress path is what a user has to change. Output that
    describes a conversion or verification problem returns None so it keeps its own class.
    """

    output = f"{stdout}\n{stderr}"
    for error_class, pattern in SOURCE_ACCESS_PATTERNS:
        if pattern.search(output):
            return error_class
    return None


def _warning_lines(output: str) -> list[str]:
    return [
        line.strip()
        for line in output.splitlines()
        if "warning" in line.casefold() or "error" in line.casefold()
    ]


def classify_warnings(stdout: str, stderr: str) -> tuple[AcquisitionWarning, ...]:
    records: list[AcquisitionWarning] = []
    seen: set[str] = set()
    for line in _warning_lines(f"{stdout}\n{stderr}"):
        normalized = " ".join(line.split())
        if normalized in seen:
            continue
        seen.add(normalized)
        category = "other"
        quality_affecting = "error" in normalized.casefold()
        for candidate_category, pattern in QUALITY_PATTERNS:
            if pattern.search(normalized):
                category = candidate_category
                quality_affecting = True
                break
        records.append(AcquisitionWarning(category, normalized, quality_affecting))
    return tuple(records)
