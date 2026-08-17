from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import json

from .tooling import CommandRunner


@dataclass(frozen=True)
class AudioStream:
    codec: str
    sample_rate_hz: int | None
    channels: int | None
    bitrate_bps: int | None


@dataclass(frozen=True)
class MediaProbe:
    format_name: str
    duration_seconds: float
    audio: AudioStream
    video_stream_count: int


def _optional_int(value: object) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer media property: {value!r}") from exc


def parse_ffprobe(data: dict[str, object], *, allow_video: bool) -> MediaProbe:
    streams = data.get("streams")
    format_data = data.get("format")
    if not isinstance(streams, list) or not isinstance(format_data, dict):
        raise ValueError("FFprobe response is missing streams or format data")
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
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
    return MediaProbe(
        format_name=str(format_data.get("format_name") or "unknown"),
        duration_seconds=duration,
        audio=AudioStream(
            codec=codec,
            sample_rate_hz=_optional_int(audio.get("sample_rate")),
            channels=_optional_int(audio.get("channels")),
            bitrate_bps=_optional_int(audio.get("bit_rate")),
        ),
        video_stream_count=len(video_streams),
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
            "format=format_name,duration:stream=index,codec_type,codec_name,sample_rate,channels,bit_rate",
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
        raise ValueError("FFprobe returned an invalid response shape")
    return parse_ffprobe(data, allow_video=allow_video)


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

