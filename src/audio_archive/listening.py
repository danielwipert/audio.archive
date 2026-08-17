from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .ableton import (
    ExistingDerivativeConflict,
    _load_manifest,
    _restore_bytes,
    _safe_item_path,
)
from .config import AppConfig
from .integrity import listed_checksum_paths, verify_sha256sums, write_sha256sums
from .manifest import write_manifest_atomic
from .tooling import (
    CommandResult,
    CommandRunner,
    ToolExecutionError,
    read_tool_version,
    resolve_tool,
)
from .verify import MediaProbe, probe_media, sha256_file

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class ListeningAsset:
    relative_path: str
    path: Path
    sha256: str
    sample_rate_hz: int
    channels: int
    bitrate_bps: int | None
    title: str
    artist: str

    def manifest_record(self, source_sha256: str, ffmpeg_version: str) -> dict[str, object]:
        return {
            "role": "listening",
            "path": self.relative_path,
            "container": "mp3",
            "audio_codec": "mp3",
            "audio_format": "mp3",
            "encoder": "libmp3lame",
            "encoder_settings": {"quality_mode": "VBR", "quality_scale": 0},
            "ffmpeg_version": ffmpeg_version,
            "reported_bitrate_kbps": (
                self.bitrate_bps / 1000 if self.bitrate_bps is not None else None
            ),
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "normalization": False,
            "resampled": False,
            "embedded_artwork": True,
            "title": self.title,
            "artist": self.artist,
            "source_sha256": source_sha256,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ListeningResult:
    archive_id: str
    item_directory: Path
    asset: ListeningAsset
    reused_existing: bool


def _curated_tags(manifest: dict[str, object]) -> tuple[str, str]:
    request = manifest.get("request")
    source = manifest.get("source")
    request_data = request if isinstance(request, dict) else {}
    source_data = source if isinstance(source, dict) else {}
    title = str(request_data.get("title") or source_data.get("title") or "Untitled").strip()
    version = str(request_data.get("version") or "").strip()
    if version and version.casefold() not in title.casefold():
        title = f"{title} ({version})"
    artist = str(
        request_data.get("artist") or source_data.get("creator") or "Unknown Artist"
    ).strip()
    return title, artist


def _find_artwork(item_directory: Path, checksum_paths: list[Path]) -> Path:
    candidates = [
        relative
        for relative in checksum_paths
        if len(relative.parts) == 2
        and relative.parts[0] == "artwork"
        and relative.name.startswith("source-thumbnail")
        and relative.suffix.casefold() in IMAGE_EXTENSIONS
    ]
    if len(candidates) != 1:
        raise ExistingDerivativeConflict(
            f"Expected one checksummed source thumbnail; found {len(candidates)}"
        )
    artwork = item_directory / candidates[0]
    if not artwork.is_file() or artwork.stat().st_size <= 0:
        raise ExistingDerivativeConflict("Source thumbnail is missing or empty")
    return artwork


def _verify_listening_probe(
    probe: MediaProbe,
    *,
    sample_rate_hz: int,
    channels: int,
    title: str,
    artist: str,
) -> None:
    if "mp3" not in probe.format_name.casefold().split(","):
        raise ValueError(f"Listening output container must be MP3, found {probe.format_name}")
    if probe.audio.codec != "mp3":
        raise ValueError(f"Listening output codec must be MP3, found {probe.audio.codec}")
    if probe.audio.sample_rate_hz != sample_rate_hz:
        raise ValueError("Listening output changed the source sample rate")
    if probe.audio.channels != channels:
        raise ValueError("Listening output changed the source channel count")
    if probe.video_stream_count != 1 or probe.attached_picture_count != 1:
        raise ValueError("Listening output must contain exactly one attached cover image")
    tags = probe.tags or {}
    if tags.get("title") != title or tags.get("artist") != artist:
        raise ValueError("Listening output does not contain the curated title and artist")


def _write_listen_log(
    path: Path,
    *,
    command_result: CommandResult,
    ffmpeg_version: str,
    ffprobe_version: str,
    source_path: Path,
    artwork_path: Path,
    title: str,
    artist: str,
    asset: ListeningAsset | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "started_at_utc": command_result.started_at_utc,
        "finished_at_utc": command_result.finished_at_utc,
        "argv": list(command_result.argv),
        "returncode": command_result.returncode,
        "stdout": command_result.stdout,
        "stderr": command_result.stderr,
        "ffmpeg_version": ffmpeg_version,
        "ffprobe_version": ffprobe_version,
        "source_path": str(source_path),
        "artwork_path": str(artwork_path),
        "encoder": "libmp3lame",
        "encoder_settings": {"quality_mode": "VBR", "quality_scale": 0},
        "metadata": {"title": title, "artist": artist},
        "output": asdict(asset) | {"path": str(asset.path)} if asset else None,
    }
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class ListeningService:
    def __init__(self, config: AppConfig, runner: CommandRunner):
        self.config = config
        self.runner = runner

    def create(self, item_directory: Path, *, job_id: int) -> ListeningResult:
        integrity = verify_sha256sums(item_directory)
        if not integrity.valid:
            raise ExistingDerivativeConflict(
                "Source archive failed integrity verification: " + "; ".join(integrity.errors)
            )
        checksum_paths = listed_checksum_paths(item_directory)
        manifest_path, manifest = _load_manifest(item_directory)
        archive_id = str(manifest.get("archive_id") or "")
        source_data = manifest.get("source_master")
        if not archive_id or not isinstance(source_data, dict):
            raise ExistingDerivativeConflict("Archive manifest has no source identity or master")
        source_relative = str(source_data.get("path") or "")
        source_path = _safe_item_path(item_directory, source_relative)
        source_sha256 = str(source_data.get("sha256") or "")
        if not source_path.is_file() or sha256_file(source_path) != source_sha256:
            raise ExistingDerivativeConflict("Source master does not match its manifest checksum")
        artwork_path = _find_artwork(item_directory, checksum_paths)
        title, artist = _curated_tags(manifest)

        ffprobe = resolve_tool(self.config.ffprobe, self.config.tools_directory)
        ffprobe_version = read_tool_version(self.runner, ffprobe, "-version")
        source_probe = probe_media(self.runner, ffprobe, source_path)
        sample_rate_hz = source_probe.audio.sample_rate_hz
        channels = source_probe.audio.channels
        if sample_rate_hz is None or channels not in {1, 2}:
            raise ValueError("Listening conversion requires a known mono or stereo source rate")

        derivatives = manifest.get("derivatives")
        if not isinstance(derivatives, list):
            raise ExistingDerivativeConflict("Manifest derivatives field is invalid")
        existing = [
            record
            for record in derivatives
            if isinstance(record, dict) and record.get("role") == "listening"
        ]
        if existing:
            if len(existing) != 1:
                raise ExistingDerivativeConflict("Manifest contains multiple listening outputs")
            asset = self._verify_existing(
                item_directory,
                existing[0],
                checksum_paths,
                source_sha256,
                ffprobe,
                sample_rate_hz,
                channels,
                title,
                artist,
            )
            return ListeningResult(archive_id, item_directory, asset, True)

        video_id = archive_id.partition(":")[2]
        if not video_id:
            raise ExistingDerivativeConflict("Archive ID does not contain a source ID")
        relative = Path("derivatives/listen") / f"{video_id}.mp3"
        final_path = item_directory / relative
        if final_path.exists():
            raise ExistingDerivativeConflict(
                "Unrecorded listening output already exists; refusing to overwrite it"
            )

        ffmpeg = resolve_tool(self.config.ffmpeg, self.config.tools_directory)
        ffmpeg_version = read_tool_version(self.runner, ffmpeg, "-version")
        job_temp = self.config.temp_directory / str(job_id) / "listening"
        if job_temp.exists():
            shutil.rmtree(job_temp)
        job_temp.mkdir(parents=True)
        generated = job_temp / f"{video_id}.mp3"
        listen_log = job_temp / "listen.log"
        command = (
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            str(source_path),
            "-i",
            str(artwork_path),
            "-map",
            "0:a:0",
            "-map",
            "1:v:0",
            "-map_metadata",
            "-1",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "0",
            "-c:v",
            "mjpeg",
            "-id3v2_version",
            "3",
            "-metadata",
            f"title={title}",
            "-metadata",
            f"artist={artist}",
            "-metadata:s:v",
            "title=Album cover",
            "-metadata:s:v",
            "comment=Cover (front)",
            "-disposition:v",
            "attached_pic",
            str(generated),
        )
        try:
            command_result = self.runner.run(command)
        except ToolExecutionError as exc:
            _write_listen_log(
                listen_log,
                command_result=exc.result,
                ffmpeg_version=ffmpeg_version,
                ffprobe_version=ffprobe_version,
                source_path=source_path,
                artwork_path=artwork_path,
                title=title,
                artist=artist,
            )
            raise
        output_probe = probe_media(self.runner, ffprobe, generated, allow_video=True)
        _verify_listening_probe(
            output_probe,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            title=title,
            artist=artist,
        )
        asset = ListeningAsset(
            relative.as_posix(),
            final_path,
            sha256_file(generated),
            sample_rate_hz,
            channels,
            output_probe.audio.bitrate_bps,
            title,
            artist,
        )
        _write_listen_log(
            listen_log,
            command_result=command_result,
            ffmpeg_version=ffmpeg_version,
            ffprobe_version=ffprobe_version,
            source_path=source_path,
            artwork_path=artwork_path,
            title=title,
            artist=artist,
            asset=asset,
        )
        return self._publish(
            item_directory=item_directory,
            manifest_path=manifest_path,
            manifest=manifest,
            checksum_paths=checksum_paths,
            source_sha256=source_sha256,
            ffmpeg_version=ffmpeg_version,
            generated=generated,
            listen_log=listen_log,
            asset=asset,
            job_id=job_id,
        )

    def _verify_existing(
        self,
        item_directory: Path,
        record: dict[str, object],
        checksum_paths: list[Path],
        source_sha256: str,
        ffprobe: str,
        sample_rate_hz: int,
        channels: int,
        title: str,
        artist: str,
    ) -> ListeningAsset:
        relative = str(record.get("path") or "")
        path = _safe_item_path(item_directory, relative)
        if relative not in {path.as_posix() for path in checksum_paths}:
            raise ExistingDerivativeConflict("Listening output is absent from SHA256SUMS")
        digest = str(record.get("sha256") or "")
        if not path.is_file() or sha256_file(path) != digest:
            raise ExistingDerivativeConflict("Listening output failed checksum verification")
        if record.get("source_sha256") != source_sha256:
            raise ExistingDerivativeConflict(
                "Listening output was not made from the current source master"
            )
        probe = probe_media(self.runner, ffprobe, path, allow_video=True)
        _verify_listening_probe(
            probe,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            title=title,
            artist=artist,
        )
        return ListeningAsset(
            relative,
            path,
            digest,
            sample_rate_hz,
            channels,
            probe.audio.bitrate_bps,
            title,
            artist,
        )

    def _publish(
        self,
        *,
        item_directory: Path,
        manifest_path: Path,
        manifest: dict[str, object],
        checksum_paths: list[Path],
        source_sha256: str,
        ffmpeg_version: str,
        generated: Path,
        listen_log: Path,
        asset: ListeningAsset,
        job_id: int,
    ) -> ListeningResult:
        original_manifest = manifest_path.read_bytes()
        checksum_file = item_directory / "checksums" / "SHA256SUMS"
        original_checksums = checksum_file.read_bytes()
        final_log = item_directory / "logs" / "listen.log"
        original_log = final_log.read_bytes() if final_log.is_file() else None
        output_root = item_directory / "derivatives" / "listen"
        staging = output_root / f".staging-{job_id}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        published: Path | None = None
        try:
            staged_file = staging / generated.name
            shutil.copy2(generated, staged_file)
            os.replace(staged_file, asset.path)
            published = asset.path
            final_log.parent.mkdir(parents=True, exist_ok=True)
            staged_log = staging / "listen.log"
            shutil.copy2(listen_log, staged_log)
            os.replace(staged_log, final_log)
            derivatives = manifest["derivatives"]
            if not isinstance(derivatives, list):
                raise ExistingDerivativeConflict("Manifest derivatives field is invalid")
            manifest["derivatives"] = derivatives + [
                asset.manifest_record(source_sha256, ffmpeg_version)
            ]
            write_manifest_atomic(manifest_path, manifest)
            new_paths = [
                Path(asset.relative_path),
                Path("logs/listen.log"),
                Path("metadata/archive.json"),
            ]
            write_sha256sums(item_directory, list(dict.fromkeys(checksum_paths + new_paths)))
            verified = verify_sha256sums(item_directory)
            if not verified.valid:
                raise ExistingDerivativeConflict(
                    "Published listening output failed integrity verification: "
                    + "; ".join(verified.errors)
                )
        except Exception:
            _restore_bytes(manifest_path, original_manifest)
            _restore_bytes(checksum_file, original_checksums)
            if published:
                published.unlink(missing_ok=True)
            if original_log is None:
                final_log.unlink(missing_ok=True)
            else:
                _restore_bytes(final_log, original_log)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return ListeningResult(str(manifest["archive_id"]), item_directory, asset, False)
