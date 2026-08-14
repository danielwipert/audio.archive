from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata


VERSION_TERMS: dict[str, tuple[str, ...]] = {
    "album": ("album", "album version"),
    "single": ("single", "single version"),
    "live": ("live", "concert", "session"),
    "remix": ("remix", "mix"),
    "instrumental": ("instrumental",),
    "remaster": ("remaster", "remastered"),
}

DISQUALIFYING_TERMS: dict[str, tuple[str, ...]] = {
    "live": ("live", "concert"),
    "remix": ("remix",),
    "cover": ("cover", "tribute"),
    "karaoke": ("karaoke",),
    "instrumental": ("instrumental",),
    "slowed": ("slowed", "slowed reverb"),
    "sped_up": ("sped up", "speed up", "nightcore"),
    "reaction": ("reaction",),
    "tutorial": ("tutorial", "how to play"),
}

OFFICIAL_TERMS = ("official audio", "official video", "provided to youtube", "topic")


@dataclass(frozen=True)
class Candidate:
    video_id: str
    title: str
    channel: str = ""
    duration_seconds: float | None = None
    url: str | None = None
    thumbnail_url: str | None = None


@dataclass(frozen=True)
class CandidateScore:
    candidate: Candidate
    score: int
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    disqualified: bool


@dataclass(frozen=True)
class ResolutionDecision:
    selected: CandidateScore | None
    ranked: tuple[CandidateScore, ...]
    method: str
    selected_score: int | None
    runner_up_score: int | None
    margin: int | None


def normalize_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    folded = "".join(character for character in folded if not unicodedata.combining(character))
    folded = re.sub(r"\b(feat|featuring|ft)\.?\b", " ", folded)
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    return " ".join(folded.split())


def _similarity(expected: str, actual: str) -> float:
    expected_normalized = normalize_text(expected)
    actual_normalized = normalize_text(actual)
    if not expected_normalized or not actual_normalized:
        return 0.0
    if expected_normalized in actual_normalized:
        return 1.0
    expected_tokens = set(expected_normalized.split())
    actual_tokens = set(actual_normalized.split())
    token_score = len(expected_tokens & actual_tokens) / len(expected_tokens)
    sequence_score = SequenceMatcher(None, expected_normalized, actual_normalized).ratio()
    return max(token_score, sequence_score)


def _contains_term(text: str, term: str) -> bool:
    normalized = f" {normalize_text(text)} "
    normalized_term = normalize_text(term)
    return f" {normalized_term} " in normalized


def score_candidate(
    *,
    artist: str,
    title: str,
    version: str | None,
    candidate: Candidate,
    expected_duration_seconds: float | None = None,
) -> CandidateScore:
    searchable = f"{candidate.title} {candidate.channel}"
    title_similarity = _similarity(title, candidate.title)
    artist_similarity = _similarity(artist, searchable)
    score = round(45 * title_similarity + 35 * artist_similarity)
    reasons = [
        f"title similarity {title_similarity:.2f}",
        f"artist similarity {artist_similarity:.2f}",
    ]
    warnings: list[str] = []

    normalized_version = normalize_text(version or "")
    if version:
        requested_terms = VERSION_TERMS.get(normalized_version, (normalized_version,))
        if any(_contains_term(searchable, term) for term in requested_terms):
            score += 10
            reasons.append(f"requested version matched: {version}")
        else:
            warnings.append(f"requested version not evident: {version}")
    else:
        score += 10
        reasons.append("no alternate version requested")

    if any(_contains_term(searchable, term) for term in OFFICIAL_TERMS):
        score += 10
        reasons.append("official-source signal")

    disqualifiers: list[str] = []
    for label, terms in DISQUALIFYING_TERMS.items():
        requested = bool(version) and (
            normalized_version == normalize_text(label)
            or any(_contains_term(version or "", term) for term in terms)
        )
        if not requested and any(_contains_term(searchable, term) for term in terms):
            disqualifiers.append(label)
    if disqualifiers:
        penalty = min(60, 25 * len(disqualifiers))
        score -= penalty
        warnings.extend(f"unrequested version term: {term}" for term in disqualifiers)

    if expected_duration_seconds and candidate.duration_seconds:
        variance = abs(candidate.duration_seconds - expected_duration_seconds) / expected_duration_seconds
        if variance <= 0.05:
            score += 5
            reasons.append("duration within 5% of expected")
        elif variance >= 0.25:
            score -= 10
            warnings.append("duration differs from expected by at least 25%")

    return CandidateScore(
        candidate=candidate,
        score=max(0, min(100, score)),
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        disqualified=bool(disqualifiers),
    )


def decide_resolution(
    *,
    artist: str,
    title: str,
    version: str | None,
    candidates: list[Candidate],
    minimum_score: int = 90,
    minimum_margin: int = 15,
    expected_duration_seconds: float | None = None,
) -> ResolutionDecision:
    ranked = tuple(
        sorted(
            (
                score_candidate(
                    artist=artist,
                    title=title,
                    version=version,
                    candidate=candidate,
                    expected_duration_seconds=expected_duration_seconds,
                )
                for candidate in candidates
            ),
            key=lambda result: (-result.score, result.candidate.video_id),
        )
    )
    if not ranked:
        return ResolutionDecision(None, (), "not_found", None, None, None)
    best = ranked[0]
    runner_up = ranked[1].score if len(ranked) > 1 else 0
    margin = best.score - runner_up
    automatic = (
        best.score >= minimum_score
        and margin >= minimum_margin
        and not best.disqualified
    )
    return ResolutionDecision(
        selected=best if automatic else None,
        ranked=ranked,
        method="automatic" if automatic else "needs_review",
        selected_score=best.score,
        runner_up_score=runner_up if len(ranked) > 1 else None,
        margin=margin,
    )

