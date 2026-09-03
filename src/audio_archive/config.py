from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    archive_root: Path
    temp_directory: Path
    database_path: Path
    host: str
    port: int
    open_browser: bool
    poll_interval_seconds: float
    safe_wav_size_gib: float
    segment_minutes: int
    tools_directory: Path
    yt_dlp: str
    ffmpeg: str
    ffprobe: str
    deno: str
    candidate_limit: int
    auto_select_min_score: int
    auto_select_min_margin: int
    max_csv_bytes: int
    pot_provider: str = "script"
    pot_http_base_url: str = "http://127.0.0.1:4416"

    def __post_init__(self) -> None:
        if self.pot_provider not in {"script", "http"}:
            raise ValueError(f"Unknown PO token provider: {self.pot_provider}")


def discover_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for path in (current, *current.parents):
        if (path / "pyproject.toml").is_file() and (path / "config" / "base.toml").is_file():
            return path
    raise FileNotFoundError("Could not locate pyproject.toml and config/base.toml")


def _project_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def load_config(project_root: Path | None = None) -> AppConfig:
    root = (project_root or discover_project_root()).resolve()
    with (root / "config" / "base.toml").open("rb") as handle:
        raw = tomllib.load(handle)

    archive_root = _project_path(root, raw["archive"]["root"])
    temp_directory = _project_path(root, raw["archive"]["temp_directory"])
    return AppConfig(
        project_root=root,
        archive_root=archive_root,
        temp_directory=temp_directory,
        database_path=archive_root / "app-data" / "archive.db",
        host=str(raw["app"]["host"]),
        port=int(raw["app"]["port"]),
        open_browser=bool(raw["app"]["open_browser"]),
        poll_interval_seconds=float(raw["app"]["poll_interval_seconds"]),
        safe_wav_size_gib=float(raw["archive"]["safe_wav_size_gib"]),
        segment_minutes=int(raw["archive"]["segment_minutes"]),
        tools_directory=_project_path(root, raw["tools"]["directory"]),
        yt_dlp=str(raw["tools"]["yt_dlp"]),
        ffmpeg=str(raw["tools"]["ffmpeg"]),
        ffprobe=str(raw["tools"]["ffprobe"]),
        deno=str(raw["tools"]["deno"]),
        candidate_limit=int(raw["resolver"]["candidate_limit"]),
        auto_select_min_score=int(raw["resolver"]["auto_select_min_score"]),
        auto_select_min_margin=int(raw["resolver"]["auto_select_min_margin"]),
        max_csv_bytes=int(raw["input"]["max_csv_bytes"]),
        pot_provider=str(raw["tools"].get("pot_provider", "script")),
        pot_http_base_url=str(
            raw["tools"].get("pot_http_base_url", "http://127.0.0.1:4416")
        ),
    )


def load_profile(name: str, project_root: Path | None = None) -> dict[str, object]:
    root = (project_root or discover_project_root()).resolve()
    if name not in {"ableton", "archive", "listen", "complete"}:
        raise ValueError(f"Unknown output profile: {name}")
    with (root / "config" / "profiles" / f"{name}.toml").open("rb") as handle:
        return tomllib.load(handle)

