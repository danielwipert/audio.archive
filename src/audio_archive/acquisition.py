from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import json
import os
import re
import shutil
from typing import Any

from .config import AppConfig
from .integrity import verify_sha256sums, write_sha256sums
from .manifest import SCHEMA_VERSION, write_manifest_atomic
from .tooling import (
    MINIMUM_DENO_VERSION,
    CommandResult,
    CommandRunner,
    ToolExecutionError,
    read_tool_version,
    resolve_tool,
)
from .urls import VIDEO_ID_PATTERN, parse_youtube_url
from .verify import AudioStream, MediaProbe, probe_media, sha256_file
from .warnings import AcquisitionWarning, classify_warnings


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
NON_MEDIA_SUFFIXES = IMAGE_EXTENSIONS | {".json", ".part", ".ytdl", ".tmp"}
BGUTIL_PROVIDER_DIRECTORY = "bgutil-ytdlp-pot-provider"


@dataclass(frozen=True)
class AcquisitionRequest:
    job_id: int
    video_id: str
    url: str
    profile: str
    artist: str | None = None
    title: str | None = None
    version: str | None = None
    origin: str = "url"
    import_filename: str | None = None
    import_file_sha256: str | None = None
    import_row: int | None = None
    resolution_method: str = "exact_url"
    resolver_version: str = "1.0"
    selected_score: int | None = None
    runner_up_score: int | None = None
    reviewed_by_user: bool = False

    def validate(self) -> None:
        if self.job_id <= 0:
            raise ValueError("job_id must be positive")
        if not VIDEO_ID_PATTERN.fullmatch(self.video_id):
            raise ValueError("Invalid pinned YouTube video ID")
        parsed = parse_youtube_url(self.url)
        if parsed.video_id != self.video_id:
            raise ValueError("Pinned URL and video ID do not match")
        if self.profile not in {"ableton", "archive", "listen", "complete"}:
            raise ValueError(f"Unknown output profile: {self.profile}")


@dataclass(frozen=True)
class SelectedFormat:
    format_id: str | None
    container: str | None
    audio_codec: str | None
    reported_bitrate_kbps: float | None
    sample_rate_hz: int | None
    channels: int | None
    is_drc: bool
    audio_only: bool
    evidence: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class AcquisitionResult:
    archive_id: str
    video_id: str
    item_directory: Path
    manifest_path: Path
    master_path: Path
    master_relative_path: str
    master_sha256: str
    quality_status: str
    warnings: tuple[AcquisitionWarning, ...]
    source_title: str
    source_creator: str | None
    probe: MediaProbe
    reused_existing: bool


class ExistingArchiveConflict(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _optional_int(value: object) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _selected_download(info: dict[str, Any]) -> dict[str, Any]:
    requested = info.get("requested_downloads")
    if isinstance(requested, list) and requested and isinstance(requested[0], dict):
        return requested[0]
    return info


def _format_evidence(info: dict[str, Any]) -> tuple[dict[str, object], ...]:
    formats = info.get("formats")
    if not isinstance(formats, list):
        return ()
    evidence: list[dict[str, object]] = []
    for item in formats:
        if not isinstance(item, dict) or item.get("acodec") in (None, "none"):
            continue
        evidence.append(
            {
                "format_id": item.get("format_id"),
                "ext": item.get("ext"),
                "acodec": item.get("acodec"),
                "vcodec": item.get("vcodec"),
                "abr": item.get("abr"),
                "asr": item.get("asr"),
                "audio_channels": item.get("audio_channels"),
                "format_note": item.get("format_note"),
            }
        )
    return tuple(evidence)


def parse_selected_format(info: dict[str, Any]) -> SelectedFormat:
    selected = _selected_download(info)
    format_note = str(selected.get("format_note") or "")
    format_id = str(selected.get("format_id") or "") or None
    is_drc = "drc" in format_note.casefold() or bool(
        format_id and format_id.casefold().endswith("-drc")
    )
    vcodec = str(selected.get("vcodec") or info.get("vcodec") or "none")
    return SelectedFormat(
        format_id=format_id,
        container=str(selected.get("ext") or info.get("ext") or "") or None,
        audio_codec=str(selected.get("acodec") or info.get("acodec") or "") or None,
        reported_bitrate_kbps=_optional_float(selected.get("abr")),
        sample_rate_hz=_optional_int(selected.get("asr")),
        channels=_optional_int(selected.get("audio_channels")),
        is_drc=is_drc,
        audio_only=vcodec == "none",
        evidence=_format_evidence(info),
    )


def _find_single(job_temp: Path, pattern: str, *, excluded_suffixes: set[str]) -> Path:
    matches = sorted(
        path
        for path in job_temp.glob(pattern)
        if path.is_file() and path.suffix.casefold() not in excluded_suffixes
    )
    if len(matches) != 1:
        raise ValueError(f"Expected one acquired media file; found {len(matches)}")
    return matches[0]


def _find_thumbnail(job_temp: Path) -> Path | None:
    images = [
        path
        for path in job_temp.glob("source.*")
        if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS
    ]
    return max(images, key=lambda path: path.stat().st_size) if images else None


def _demux_extension(probe: MediaProbe) -> str:
    codec = probe.audio.codec.casefold()
    if codec in {"aac", "alac"}:
        return ".m4a"
    if codec in {"opus", "vorbis"}:
        return ".webm"
    if codec == "mp3":
        return ".mp3"
    if codec == "flac":
        return ".flac"
    return ".mka"


def _write_ingest_log(
    path: Path,
    *,
    command_result: CommandResult,
    tool_versions: dict[str, str | None],
    warnings: tuple[AcquisitionWarning, ...],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "started_at_utc": command_result.started_at_utc,
        "finished_at_utc": command_result.finished_at_utc,
        "argv": list(command_result.argv),
        "returncode": command_result.returncode,
        "stdout": command_result.stdout,
        "stderr": command_result.stderr,
        "tool_versions": tool_versions,
        "warnings": [warning.__dict__ for warning in warnings],
    }
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _existing_result(item_directory: Path, video_id: str) -> AcquisitionResult | None:
    manifest_path = item_directory / "metadata" / "archive.json"
    if not manifest_path.is_file():
        return None
    integrity = verify_sha256sums(item_directory)
    if not integrity.valid:
        raise ExistingArchiveConflict(
            "Existing archive item failed integrity verification: " + "; ".join(integrity.errors)
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    master_data = manifest.get("source_master") or {}
    relative = str(master_data.get("path") or "")
    relative_path = Path(relative)
    if not relative or relative_path.is_absolute() or ".." in relative_path.parts:
        raise ExistingArchiveConflict("Existing manifest contains an unsafe source-master path")
    master_path = item_directory / relative_path
    if not master_path.is_file():
        raise ExistingArchiveConflict("Existing manifest references a missing source master")
    probe_data = master_data.get("verified_probe") or {}
    audio_data = probe_data.get("audio") or {}
    probe = MediaProbe(
        format_name=str(probe_data.get("format_name") or master_data.get("container") or "unknown"),
        duration_seconds=float(probe_data.get("duration_seconds") or 0),
        audio=AudioStream(
            codec=str(audio_data.get("codec") or master_data.get("audio_codec") or "unknown"),
            sample_rate_hz=_optional_int(audio_data.get("sample_rate_hz")),
            channels=_optional_int(audio_data.get("channels")),
            bitrate_bps=_optional_int(audio_data.get("bitrate_bps")),
        ),
        video_stream_count=0,
    )
    source = manifest.get("source") or {}
    acquisition = manifest.get("acquisition") or {}
    existing_warnings = tuple(
        AcquisitionWarning("recorded", str(message), True)
        for message in acquisition.get("quality_warnings", [])
        if message
    )
    return AcquisitionResult(
        archive_id=f"youtube:{video_id}",
        video_id=video_id,
        item_directory=item_directory,
        manifest_path=manifest_path,
        master_path=master_path,
        master_relative_path=relative,
        master_sha256=str(master_data.get("sha256") or sha256_file(master_path)),
        quality_status=str(acquisition.get("quality_status") or "best_available_with_warnings"),
        warnings=existing_warnings,
        source_title=str(source.get("title") or video_id),
        source_creator=source.get("creator"),
        probe=probe,
        reused_existing=True,
    )


class AcquisitionService:
    def __init__(self, config: AppConfig, runner: CommandRunner):
        self.config = config
        self.runner = runner

    def _pot_extractor_arg(self) -> str:
        """Point yt-dlp at whichever BgUtils provider this deployment runs.

        The script provider fetches YouTube's homepage itself for every token, which a
        slow egress path can fail inside the provider's fixed timeout. The HTTP server
        reuses the page yt-dlp already fetched and keeps its session warm, so a
        deployment behind a proxy prefers it.
        """

        if self.config.pot_provider == "http":
            return f"youtubepot-bgutilhttp:base_url={self.config.pot_http_base_url}"
        server = self.config.tools_directory / BGUTIL_PROVIDER_DIRECTORY / "server"
        if not (server / "src" / "main.ts").is_file():
            raise FileNotFoundError(
                "YouTube PO token provider is not installed. Run scripts\\setup.ps1 again."
            )
        return f"youtubepot-bgutilscript:server_home={server}"

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        request.validate()
        item_directory = self.config.archive_root / "items" / "youtube" / request.video_id
        if item_directory.exists():
            existing = _existing_result(item_directory, request.video_id)
            if existing:
                return existing
            raise ExistingArchiveConflict(
                f"Archive directory exists without a valid manifest: {item_directory}"
            )

        yt_dlp = resolve_tool(self.config.yt_dlp, self.config.tools_directory)
        ffmpeg = resolve_tool(self.config.ffmpeg, self.config.tools_directory)
        ffprobe = resolve_tool(self.config.ffprobe, self.config.tools_directory)
        deno = resolve_tool(self.config.deno, self.config.tools_directory)
        pot_extractor_arg = self._pot_extractor_arg()

        job_temp = self.config.temp_directory / str(request.job_id)
        job_temp.mkdir(parents=True, exist_ok=True)
        info_path = job_temp / "source.info.json"
        versions: dict[str, str | None] = {
            "yt_dlp": read_tool_version(self.runner, yt_dlp, "--version"),
            "ffmpeg": read_tool_version(self.runner, ffmpeg, "-version"),
            "ffprobe": read_tool_version(self.runner, ffprobe, "-version"),
            "deno": read_tool_version(self.runner, deno, "--version"),
        }
        deno_match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", versions["deno"] or "")
        if not deno_match or tuple(map(int, deno_match.groups())) < MINIMUM_DENO_VERSION:
            minimum = ".".join(map(str, MINIMUM_DENO_VERSION))
            raise ValueError(
                f"Deno {minimum} or newer is required for full YouTube format access"
            )
        command = (
            yt_dlp,
            "--ignore-config",
            "--no-playlist",
            "--continue",
            "--no-overwrites",
            "--no-progress",
            "--write-info-json",
            "--write-thumbnail",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--extractor-retries",
            "2",
            "--js-runtimes",
            f"deno:{deno}",
            "--extractor-args",
            pot_extractor_arg,
            "--paths",
            str(job_temp),
            "--output",
            "source.%(ext)s",
            "--format",
            "bestaudio/best",
            request.url,
        )
        try:
            command_result = self.runner.run(command)
        except ToolExecutionError as exc:
            failed_warnings = classify_warnings(exc.result.stdout, exc.result.stderr)
            _write_ingest_log(
                job_temp / "ingest.log",
                command_result=exc.result,
                tool_versions=versions,
                warnings=failed_warnings,
            )
            raise
        warnings = list(classify_warnings(command_result.stdout, command_result.stderr))
        _write_ingest_log(
            job_temp / "ingest.log",
            command_result=command_result,
            tool_versions=versions,
            warnings=tuple(warnings),
        )
        if not info_path.is_file():
            raise ValueError("yt-dlp did not create source.info.json")
        raw_info = info_path.read_bytes()
        try:
            info = json.loads(raw_info)
        except json.JSONDecodeError as exc:
            raise ValueError("yt-dlp source info JSON is invalid") from exc
        if not isinstance(info, dict):
            raise ValueError("yt-dlp source info must be a JSON object")
        extracted_id = str(info.get("id") or "")
        if extracted_id != request.video_id:
            raise ValueError(
                f"Pinned source changed: expected {request.video_id}, "
                f"received {extracted_id or 'none'}"
            )

        selected = parse_selected_format(info)
        source_media = _find_single(job_temp, "source.*", excluded_suffixes=NON_MEDIA_SUFFIXES)
        initial_probe = probe_media(self.runner, ffprobe, source_media, allow_video=True)
        used_combined_fallback = initial_probe.video_stream_count > 0 or not selected.audio_only
        master_media = source_media
        if used_combined_fallback:
            demuxed = job_temp / f"demuxed{_demux_extension(initial_probe)}"
            if demuxed.exists():
                try:
                    probe_media(self.runner, ffprobe, demuxed)
                except ValueError:
                    demuxed.unlink()
            if not demuxed.exists():
                self.runner.run(
                    (
                        ffmpeg,
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-n",
                        "-i",
                        str(source_media),
                        "-map",
                        "0:a:0",
                        "-c:a",
                        "copy",
                        "-vn",
                        str(demuxed),
                    )
                )
            master_media = demuxed
        verified_probe = probe_media(self.runner, ffprobe, master_media)
        if selected.is_drc:
            warnings.append(
                AcquisitionWarning("drc", "Selected format is marked as DRC", True)
            )
        warning_tuple = tuple(warnings)
        _write_ingest_log(
            job_temp / "ingest.log",
            command_result=command_result,
            tool_versions=versions,
            warnings=warning_tuple,
        )
        quality_status = (
            "fallback_source"
            if used_combined_fallback
            else "best_available_with_warnings"
            if any(warning.quality_affecting for warning in warning_tuple)
            else "verified_best_available"
        )

        staging = job_temp / "publish"
        if staging.exists():
            if staging.parent != job_temp:
                raise ValueError("Unsafe staging directory")
            shutil.rmtree(staging)
        master_relative = Path("master") / f"{request.video_id}{master_media.suffix.casefold()}"
        master_destination = staging / master_relative
        master_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(master_media, master_destination)
        source_info_destination = staging / "metadata" / "source.info.json"
        source_info_destination.parent.mkdir(parents=True, exist_ok=True)
        source_info_destination.write_bytes(raw_info)

        thumbnail = _find_thumbnail(job_temp)
        if thumbnail is None:
            raise ValueError("yt-dlp did not create a source thumbnail")
        preservation_paths = [master_relative, Path("metadata/source.info.json")]
        artwork_relative = Path("artwork") / f"source-thumbnail{thumbnail.suffix.casefold()}"
        artwork_destination = staging / artwork_relative
        artwork_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(thumbnail, artwork_destination)
        preservation_paths.append(artwork_relative)

        master_digest = sha256_file(master_destination)
        source_title = str(info.get("title") or request.video_id)
        source_creator_value = info.get("channel") or info.get("uploader")
        source_creator = str(source_creator_value) if source_creator_value else None
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "archive_id": f"youtube:{request.video_id}",
            "content_type": "song",
            "request": {
                "artist": request.artist,
                "title": request.title,
                "version": request.version,
                "url": request.url,
                "profile": request.profile,
                "origin": request.origin,
                "import_filename": request.import_filename,
                "import_file_sha256": request.import_file_sha256,
                "import_row": request.import_row,
            },
            "resolution": {
                "method": request.resolution_method,
                "resolver_version": request.resolver_version,
                "selected_score": request.selected_score,
                "runner_up_score": request.runner_up_score,
                "selected_video_id": request.video_id,
                "reviewed_by_user": request.reviewed_by_user,
            },
            "source": {
                "platform": "youtube",
                "id": request.video_id,
                "url": request.url,
                "title": source_title,
                "creator": source_creator,
                "duration_seconds": verified_probe.duration_seconds,
            },
            "acquisition": {
                "acquired_at_utc": _utc_now(),
                "quality_status": quality_status,
                "yt_dlp_version": versions["yt_dlp"],
                "ffmpeg_version": versions["ffmpeg"],
                "ffprobe_version": versions["ffprobe"],
                "deno_version": versions["deno"],
                "quality_warnings": [warning.message for warning in warning_tuple],
                "used_combined_fallback": used_combined_fallback,
            },
            "source_master": {
                "role": "source_master",
                "path": master_relative.as_posix(),
                "format_id": selected.format_id,
                "container": selected.container or verified_probe.format_name,
                "audio_codec": selected.audio_codec or verified_probe.audio.codec,
                "is_drc": selected.is_drc,
                "reported_bitrate_kbps": selected.reported_bitrate_kbps,
                "sample_rate_hz": verified_probe.audio.sample_rate_hz,
                "channels": verified_probe.audio.channels,
                "sha256": master_digest,
                "selection_evidence": list(selected.evidence),
                "verified_probe": {
                    "format_name": verified_probe.format_name,
                    "duration_seconds": verified_probe.duration_seconds,
                    "audio": {
                        "codec": verified_probe.audio.codec,
                        "sample_rate_hz": verified_probe.audio.sample_rate_hz,
                        "channels": verified_probe.audio.channels,
                        "bitrate_bps": verified_probe.audio.bitrate_bps,
                    },
                    "video_stream_count": verified_probe.video_stream_count,
                },
            },
            "intermediates": [],
            "derivatives": [],
        }
        manifest_destination = staging / "metadata" / "archive.json"
        write_manifest_atomic(manifest_destination, manifest)
        preservation_paths.append(Path("metadata/archive.json"))
        ingest_log_destination = staging / "logs" / "ingest.log"
        ingest_log_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(job_temp / "ingest.log", ingest_log_destination)
        preservation_paths.append(Path("logs/ingest.log"))
        write_sha256sums(staging, preservation_paths)
        integrity = verify_sha256sums(staging)
        if not integrity.valid:
            raise ValueError("Staged archive item failed integrity verification")

        item_directory.parent.mkdir(parents=True, exist_ok=True)
        if item_directory.exists():
            raise ExistingArchiveConflict(
                f"Archive item appeared during publication: {item_directory}"
            )
        os.replace(staging, item_directory)
        return AcquisitionResult(
            archive_id=f"youtube:{request.video_id}",
            video_id=request.video_id,
            item_directory=item_directory,
            manifest_path=item_directory / "metadata" / "archive.json",
            master_path=item_directory / master_relative,
            master_relative_path=master_relative.as_posix(),
            master_sha256=master_digest,
            quality_status=quality_status,
            warnings=warning_tuple,
            source_title=source_title,
            source_creator=source_creator,
            probe=verified_probe,
            reused_existing=False,
        )
