from __future__ import annotations

from dataclasses import dataclass
from math import ceil


GIB = 1024**3


@dataclass(frozen=True)
class AbletonOutputPlan:
    estimated_bytes: int
    segmented: bool
    segment_count: int
    segment_seconds: int | None


def estimate_pcm_bytes(
    *, duration_seconds: float, sample_rate_hz: int, channels: int, bits_per_sample: int = 32
) -> int:
    if duration_seconds < 0:
        raise ValueError("duration_seconds cannot be negative")
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    if channels not in {1, 2}:
        raise ValueError("Ableton intermediate policy supports mono or stereo")
    if bits_per_sample <= 0 or bits_per_sample % 8:
        raise ValueError("bits_per_sample must be a positive multiple of 8")
    return ceil(duration_seconds * sample_rate_hz * channels * (bits_per_sample / 8))


def plan_ableton_output(
    *,
    duration_seconds: float,
    sample_rate_hz: int,
    channels: int,
    safe_size_gib: float = 1.8,
    segment_minutes: int = 60,
) -> AbletonOutputPlan:
    if safe_size_gib <= 0:
        raise ValueError("safe_size_gib must be positive")
    if segment_minutes <= 0:
        raise ValueError("segment_minutes must be positive")
    estimated = estimate_pcm_bytes(
        duration_seconds=duration_seconds,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
    )
    segment_seconds = segment_minutes * 60
    segmented = estimated > safe_size_gib * GIB
    return AbletonOutputPlan(
        estimated_bytes=estimated,
        segmented=segmented,
        segment_count=ceil(duration_seconds / segment_seconds) if segmented else 1,
        segment_seconds=segment_seconds if segmented else None,
    )

