from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlparse


VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


@dataclass(frozen=True)
class YouTubeURL:
    video_id: str
    canonical_url: str


def parse_youtube_url(value: str) -> YouTubeURL:
    raw = value.strip()
    if not raw:
        raise ValueError("YouTube URL is empty")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError("URL must use youtube.com, music.youtube.com, or youtu.be")

    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    elif parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
        video_id = parsed.path.strip("/").split("/", 2)[1]
    else:
        raise ValueError("URL does not identify a single YouTube video")

    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise ValueError("URL contains an invalid YouTube video ID")
    return YouTubeURL(video_id=video_id, canonical_url=f"https://www.youtube.com/watch?v={video_id}")

