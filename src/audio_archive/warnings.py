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
)


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
        quality_affecting = False
        for candidate_category, pattern in QUALITY_PATTERNS:
            if pattern.search(normalized):
                category = candidate_category
                quality_affecting = True
                break
        records.append(AcquisitionWarning(category, normalized, quality_affecting))
    return tuple(records)

