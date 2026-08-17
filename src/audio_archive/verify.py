from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

from .tooling import CommandRunner


@dataclass(frozen=True)
class AudioStream:
    codec: str
    sample_rate_hz: int | None
    channels: int | None
    bitrate_bps: int | None
    sample_count: int | None = None


@dataclass(frozen=True)
class MediaProbe:
    format_name: str
    duration_seconds: float
    audio: AudioStream
    video_stream_count: int
    attached_picture_count: int = 0
    tags: dict[str, str] | None = None


def _optional_int(value: object) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer media property: {value!r}") from exc


def _sample_count(audio: dict[str, object], sample_rate_hz: int | None) -> int | None:
    duration_ts = _optional_int(audio.get("duration_ts"))
    time_base = str(audio.get("time_base") or "")
    if duration_ts is None or sample_rate_hz is None or "/" not in time_base:
        return None
    try:
        count = Fraction(time_base) * duration_ts * sample_rate_hz
    except (ValueError, ZeroDivisionError):
        return None
    return int(count) if count.denominator == 1 else round(float(count))


def parse_ffprobe(data: dict[str, object], *, allow_video: bool) -> MediaProbe:
    streams = data.get("streams")
    format_data = data.get("format")
    if not isinstance(streams, list) or not isinstance(format_data, dict):
        raise ValueError(  # noqa: TRY004 - malformed external data is a value error
            "FFprobe response is missing streams or format data"
        )
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    attached_pictures = [
        item
        for item in video_streams
        if isinstance(item.get("disposition"), dict)
        and int(item["disposition"].get("attached_pic") or 0) == 1
    ]
    if len(audio_streams) != 1:
        raise ValueError(f"Expected exactly one audio stream; found {len(audio_streams)}")
    if video_streams and not allow_video:
        raise ValueError("Source master contains an unexpected video stream")
    duration_raw = format_data.get("duration")
    try:
        duration = float(str(duration_raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("FFprobe did not report a valid duration") from exc
    if duration <= 0:
        raise ValueError("Media duration must be greater than zero")
    audio = audio_streams[0]
    codec = str(audio.get("codec_name") or "").strip()
    if not codec:
        raise ValueError("FFprobe did not report an audio codec")
    sample_rate_hz = _optional_int(audio.get("sample_rate"))
    raw_tags = format_data.get("tags")
    tags = (
        {str(key).casefold(): str(value) for key, value in raw_tags.items()}
        if isinstance(raw_tags, dict)
        else {}
    )
    return MediaProbe(
        format_name=str(format_data.get("format_name") or "unknown"),
        duration_seconds=duration,
        audio=AudioStream(
            codec=codec,
            sample_rate_hz=sample_rate_hz,
            channels=_optional_int(audio.get("channels")),
            bitrate_bps=_optional_int(audio.get("bit_rate")),
            sample_count=_sample_count(audio, sample_rate_hz),
        ),
        video_stream_count=len(video_streams),
        attached_picture_count=len(attached_pictures),
        tags=tags,
    )


def probe_media(
    runner: CommandRunner,
    ffprobe: str,
    path: Path,
    *,
    allow_video: bool = False,
) -> MediaProbe:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"Media file is missing or empty: {path}")
    result = runner.run(
        (
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration:format_tags=title,artist:stream=index,codec_type,codec_name,sample_rate,channels,bit_rate,duration_ts,time_base:stream_disposition=attached_pic",
            "-of",
            "json",
            str(path),
        )
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("FFprobe returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(  # noqa: TRY004 - malformed external data is a value error
            "FFprobe returned an invalid response shape"
        )
    return parse_ffprobe(data, allow_video=allow_video)


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
