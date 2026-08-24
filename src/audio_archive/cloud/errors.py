from __future__ import annotations

import re
from dataclasses import dataclass

from ..tooling import ToolExecutionError


@dataclass(frozen=True)
class FailureClassification:
    error_class: str
    summary: str


def classify_processing_failure(stage: str, error: BaseException) -> FailureClassification:
    normalized_stage = stage.strip().casefold()
    raw = _diagnostic(error)
    text = raw.casefold()

    if normalized_stage in {"resolving", "downloading"}:
        if _contains_any(text, "po token", "proof of origin", "proof-of-origin", "pot provider"):
            error_class = "youtube_po_token"
        elif _contains_any(text, "http error 403", "403 forbidden", "403: forbidden", "status code 403"):
            error_class = "youtube_access_403"
        elif _contains_any(text, "sign in to confirm", "login required", "authentication required"):
            error_class = "youtube_auth_required"
        elif _contains_any(
            text,
            "video unavailable",
            "this video is unavailable",
            "private video",
            "video has been removed",
        ):
            error_class = "source_unavailable"
        else:
            error_class = "source_access"
    elif normalized_stage == "verifying_master":
        error_class = "source_verification"
    elif normalized_stage in {"converting", "verifying_output"}:
        error_class = "output_processing"
    elif normalized_stage == "packaging":
        error_class = "package_creation"
    elif normalized_stage == "publishing":
        error_class = "delivery_publication"
    else:
        error_class = "processing"

    summary = _redact(raw.strip())[:4000] or error_class
    return FailureClassification(error_class=error_class, summary=summary)


def _diagnostic(error: BaseException) -> str:
    if isinstance(error, ToolExecutionError):
        result = error.result
        pieces = [str(error), result.stdout.strip(), result.stderr.strip()]
        return "\n".join(piece for piece in pieces if piece)
    return str(error)


def _redact(text: str) -> str:
    patterns = (
        (r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)", r"\1[REDACTED]"),
        (r"(?i)(cookie\s*[:=]\s*)([^\s,;]+)", r"\1[REDACTED]"),
        (r"(?i)((?:access[_-]?key|secret[_-]?key|api[_-]?key|password)\s*[:=]\s*)([^\s,;]+)", r"\1[REDACTED]"),
        (r"(?i)([?&](?:X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)=)[^&\s]+", r"\1[REDACTED]"),
        (r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+", r"\1[REDACTED]"),
    )
    redacted = text
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted)
    return redacted


def _contains_any(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)
