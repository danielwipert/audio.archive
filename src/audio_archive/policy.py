from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor

GIB = 1024**3
WAV_HEADER_RESERVE = 64 * 1024


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
    safe_bytes = floor(safe_size_gib * GIB)
    bytes_per_second = sample_rate_hz * channels * 4
    default_segment_seconds = segment_minutes * 60
    maximum_safe_seconds = floor((safe_bytes - WAV_HEADER_RESERVE) / bytes_per_second)
    if maximum_safe_seconds < 1:
        raise ValueError("safe_size_gib is too small for one second of PCM audio")
    segment_seconds = min(default_segment_seconds, maximum_safe_seconds)
    segmented = estimated + WAV_HEADER_RESERVE > safe_bytes
    return AbletonOutputPlan(
        estimated_bytes=estimated,
        segmented=segmented,
        segment_count=ceil(duration_seconds / segment_seconds) if segmented else 1,
        segment_seconds=segment_seconds if segmented else None,
    )
