from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .config import AppConfig
from .integrity import listed_checksum_paths, verify_sha256sums, write_sha256sums
from .manifest import write_manifest_atomic
from .policy import ABLETON_VARIANT, GIB, AbletonOutputPlan, PcmVariant, plan_ableton_output
from .tooling import (
    CommandResult,
    CommandRunner,
    ToolExecutionError,
    read_tool_version,
    resolve_tool,
)
from .verify import MediaProbe, probe_media, sha256_file


@dataclass(frozen=True)
class PcmAsset:
    relative_path: str
    path: Path
    sha256: str
    sample_rate_hz: int
    channels: int
    sample_count: int
    start_sample: int
    end_sample: int
    segment_index: int | None
    role: str = ABLETON_VARIANT.role
    audio_format: str = ABLETON_VARIANT.codec

    def manifest_record(self, source_sha256: str) -> dict[str, object]:
        return {
            "role": self.role,
            "path": self.relative_path,
            "audio_format": self.audio_format,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "normalization": False,
            "resampled": False,
            "dithered": False,
            "source_sha256": source_sha256,
            "sha256": self.sha256,
            "segment_index": self.segment_index,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "duration_seconds": self.sample_count / self.sample_rate_hz,
        }


@dataclass(frozen=True)
class PcmResult:
    archive_id: str
    item_directory: Path
    assets: tuple[PcmAsset, ...]
    segmented: bool
    reused_existing: bool


# The Ableton intermediate is the original and most-used output of this engine, so the
# established names stay valid for every existing caller.
AbletonAsset = PcmAsset
AbletonResult = PcmResult


class ExistingDerivativeConflict(RuntimeError):
    pass


def _safe_item_path(item_directory: Path, relative_text: str) -> Path:
    relative = PurePosixPath(relative_text)
    if not relative_text or relative.is_absolute() or ".." in relative.parts:
        raise ExistingDerivativeConflict(f"Unsafe archive asset path: {relative_text!r}")
    return item_directory.joinpath(*relative.parts)


def _load_manifest(item_directory: Path) -> tuple[Path, dict[str, object]]:
    path = item_directory / "metadata" / "archive.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExistingDerivativeConflict("Archive manifest is invalid JSON") from exc
    if not isinstance(data, dict):
        raise ExistingDerivativeConflict("Archive manifest must be a JSON object")
    return path, data


def _verify_pcm_probe(
    probe: MediaProbe,
    *,
    variant: PcmVariant,
    sample_rate_hz: int,
    channels: int,
) -> int:
    if "wav" not in probe.format_name.casefold().split(","):
        raise ValueError(f"{variant.label} output container must be WAV, found {probe.format_name}")
    if probe.audio.codec != variant.codec:
        raise ValueError(
            f"{variant.label} output codec must be {variant.codec}, found {probe.audio.codec}"
        )
    if probe.audio.sample_rate_hz != sample_rate_hz:
        raise ValueError(f"{variant.label} output changed the source sample rate")
    if probe.audio.channels != channels:
        raise ValueError(f"{variant.label} output changed the source channel count")
    if probe.audio.sample_count is None or probe.audio.sample_count <= 0:
        raise ValueError(f"FFprobe did not report an exact {variant.label} output sample count")
    return probe.audio.sample_count


def _write_convert_log(
    path: Path,
    *,
    command_result: CommandResult,
    plan: AbletonOutputPlan,
    ffmpeg_version: str,
    ffprobe_version: str,
    outputs: tuple[AbletonAsset, ...] = (),
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
        "plan": asdict(plan),
        "outputs": [asdict(asset) | {"path": str(asset.path)} for asset in outputs],
    }
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _restore_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".rollback")
    temporary.write_bytes(data)
    temporary.replace(path)


class PcmWavService:
    """Decode a verified source master into uncompressed WAV under one PCM variant.

    Every rule DEC-008 fixes for the Ableton intermediate applies to each variant: the
    decode reads only the local master, applies no filter, resampling, channel remap,
    normalization or dither, and long-form output is segmented on exact sample
    boundaries and verified before publication.
    """

    def __init__(
        self,
        config: AppConfig,
        runner: CommandRunner,
        variant: PcmVariant = ABLETON_VARIANT,
    ):
        self.config = config
        self.runner = runner
        self.variant = variant

    def create(self, item_directory: Path, *, job_id: int) -> PcmResult:
        integrity = verify_sha256sums(item_directory)
        if not integrity.valid:
            raise ExistingDerivativeConflict(
                "Source archive failed integrity verification: " + "; ".join(integrity.errors)
            )
        checksum_paths = listed_checksum_paths(item_directory)
        manifest_path, manifest = _load_manifest(item_directory)
        archive_id = str(manifest.get("archive_id") or "")
        if not archive_id:
            raise ExistingDerivativeConflict("Archive manifest has no archive_id")
        source_data = manifest.get("source_master")
        if not isinstance(source_data, dict):
            raise ExistingDerivativeConflict("Archive manifest has no source master")
        source_relative = str(source_data.get("path") or "")
        source_path = _safe_item_path(item_directory, source_relative)
        source_sha256 = str(source_data.get("sha256") or "")
        if not source_path.is_file() or sha256_file(source_path) != source_sha256:
            raise ExistingDerivativeConflict("Source master does not match its manifest checksum")

        ffprobe = resolve_tool(self.config.ffprobe, self.config.tools_directory)
        ffprobe_version = read_tool_version(self.runner, ffprobe, "-version")
        source_probe = probe_media(self.runner, ffprobe, source_path)
        sample_rate_hz = source_probe.audio.sample_rate_hz
        channels = source_probe.audio.channels
        if sample_rate_hz is None or channels not in {1, 2}:
            raise ValueError(
                f"{self.variant.label} conversion requires a known mono or stereo source rate"
            )

        section = manifest.get(self.variant.manifest_section)
        if not isinstance(section, list):
            raise ExistingDerivativeConflict(
                f"Manifest {self.variant.manifest_section} field is invalid"
            )
        existing_records = [
            record
            for record in section
            if isinstance(record, dict) and record.get("role") == self.variant.role
        ]
        if existing_records:
            assets = self._verify_existing(
                item_directory,
                existing_records,
                checksum_paths,
                source_sha256,
                ffprobe,
                sample_rate_hz,
                channels,
            )
            return PcmResult(
                archive_id,
                item_directory,
                assets,
                len(assets) > 1,
                True,
            )

        video_id = archive_id.partition(":")[2]
        if not video_id:
            raise ExistingDerivativeConflict("Archive ID does not contain a source ID")
        output_root = item_directory / self.variant.output_subpath
        final_single = output_root / f"{video_id}.wav"
        final_segments = output_root / "segments"
        if final_single.exists() or final_segments.exists():
            raise ExistingDerivativeConflict(
                f"Unrecorded {self.variant.label} output already exists; refusing to overwrite it"
            )

        plan = plan_ableton_output(
            duration_seconds=source_probe.duration_seconds,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            safe_size_gib=self.config.safe_wav_size_gib,
            segment_minutes=self.config.segment_minutes,
            bits_per_sample=self.variant.bits_per_sample,
        )
        ffmpeg = resolve_tool(self.config.ffmpeg, self.config.tools_directory)
        ffmpeg_version = read_tool_version(self.runner, ffmpeg, "-version")
        job_temp = self.config.temp_directory / str(job_id) / self.variant.role
        if job_temp.exists():
            shutil.rmtree(job_temp)
        job_temp.mkdir(parents=True)
        convert_log = job_temp / self.variant.log_name
        if plan.segmented:
            generated_root = job_temp / "segments"
            generated_root.mkdir()
            output_pattern = generated_root / f"{video_id}.part-%03d.wav"
            command = (
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-n",
                "-i",
                str(source_path),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-c:a",
                self.variant.codec,
                "-f",
                "segment",
                "-segment_time",
                str(plan.segment_seconds),
                "-segment_start_number",
                "1",
                "-reset_timestamps",
                "1",
                str(output_pattern),
            )
        else:
            generated_root = job_temp
            output_path = generated_root / f"{video_id}.wav"
            command = (
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-n",
                "-i",
                str(source_path),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-c:a",
                self.variant.codec,
                str(output_path),
            )
        try:
            command_result = self.runner.run(command)
        except ToolExecutionError as exc:
            _write_convert_log(
                convert_log,
                command_result=exc.result,
                plan=plan,
                ffmpeg_version=ffmpeg_version,
                ffprobe_version=ffprobe_version,
            )
            raise

        generated = sorted(generated_root.glob(f"{video_id}*.wav"))
        if not generated:
            raise ValueError(f"FFmpeg did not create a {self.variant.label} WAV")
        assets = self._verify_generated(
            generated,
            segmented=plan.segmented,
            item_directory=item_directory,
            video_id=video_id,
            ffprobe=ffprobe,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            safe_bytes=int(self.config.safe_wav_size_gib * GIB),
        )
        if plan.segmented and len(assets) < 2:
            raise ValueError("Long-form conversion did not create multiple segments")
        _write_convert_log(
            convert_log,
            command_result=command_result,
            plan=plan,
            ffmpeg_version=ffmpeg_version,
            ffprobe_version=ffprobe_version,
            outputs=assets,
        )
        return self._publish(
            item_directory=item_directory,
            manifest_path=manifest_path,
            manifest=manifest,
            checksum_paths=checksum_paths,
            source_sha256=source_sha256,
            convert_log=convert_log,
            generated=generated,
            assets=assets,
            plan=plan,
            job_id=job_id,
        )

    def _verify_existing(
        self,
        item_directory: Path,
        records: list[dict[str, object]],
        checksum_paths: list[Path],
        source_sha256: str,
        ffprobe: str,
        sample_rate_hz: int,
        channels: int,
    ) -> tuple[PcmAsset, ...]:
        listed = {path.as_posix() for path in checksum_paths}
        assets: list[PcmAsset] = []
        expected_start = 0
        segmented = len(records) > 1
        for position, record in enumerate(records, 1):
            relative = str(record.get("path") or "")
            path = _safe_item_path(item_directory, relative)
            if relative not in listed:
                raise ExistingDerivativeConflict(
                    f"{self.variant.label} output is absent from SHA256SUMS: {relative}"
                )
            digest = str(record.get("sha256") or "")
            if not path.is_file() or sha256_file(path) != digest:
                raise ExistingDerivativeConflict(
                    f"{self.variant.label} output failed checksum verification: {relative}"
                )
            if record.get("source_sha256") != source_sha256:
                raise ExistingDerivativeConflict(
                    f"{self.variant.label} output was not made from the current source master"
                )
            sample_count = _verify_pcm_probe(
                probe_media(self.runner, ffprobe, path),
                variant=self.variant,
                sample_rate_hz=sample_rate_hz,
                channels=channels,
            )
            start_sample = int(record.get("start_sample", expected_start))
            end_sample = int(record.get("end_sample", start_sample + sample_count))
            segment_index = int(record["segment_index"]) if record.get("segment_index") else None
            if start_sample != expected_start or end_sample - start_sample != sample_count:
                raise ExistingDerivativeConflict(
                    f"{self.variant.label} segment boundaries are not contiguous"
                )
            if segmented and segment_index != position:
                raise ExistingDerivativeConflict(f"{self.variant.label} segment order is invalid")
            assets.append(
                PcmAsset(
                    relative,
                    path,
                    digest,
                    sample_rate_hz,
                    channels,
                    sample_count,
                    start_sample,
                    end_sample,
                    segment_index,
                    self.variant.role,
                    self.variant.codec,
                )
            )
            expected_start = end_sample
        return tuple(assets)

    def _verify_generated(
        self,
        generated: list[Path],
        *,
        segmented: bool,
        item_directory: Path,
        video_id: str,
        ffprobe: str,
        sample_rate_hz: int,
        channels: int,
        safe_bytes: int,
    ) -> tuple[PcmAsset, ...]:
        assets: list[PcmAsset] = []
        start_sample = 0
        for position, path in enumerate(generated, 1):
            if path.stat().st_size > safe_bytes:
                raise ValueError(
                    f"{self.variant.label} output exceeds the configured safe size: {path.name}"
                )
            sample_count = _verify_pcm_probe(
                probe_media(self.runner, ffprobe, path),
                variant=self.variant,
                sample_rate_hz=sample_rate_hz,
                channels=channels,
            )
            end_sample = start_sample + sample_count
            relative = (
                Path(self.variant.output_subpath) / "segments" / path.name
                if segmented
                else Path(self.variant.output_subpath) / f"{video_id}.wav"
            )
            assets.append(
                PcmAsset(
                    relative.as_posix(),
                    item_directory / relative,
                    sha256_file(path),
                    sample_rate_hz,
                    channels,
                    sample_count,
                    start_sample,
                    end_sample,
                    position if segmented else None,
                    self.variant.role,
                    self.variant.codec,
                )
            )
            start_sample = end_sample
        return tuple(assets)

    def _publish(
        self,
        *,
        item_directory: Path,
        manifest_path: Path,
        manifest: dict[str, object],
        checksum_paths: list[Path],
        source_sha256: str,
        convert_log: Path,
        generated: list[Path],
        assets: tuple[PcmAsset, ...],
        plan: AbletonOutputPlan,
        job_id: int,
    ) -> PcmResult:
        original_manifest = manifest_path.read_bytes()
        checksum_file = item_directory / "checksums" / "SHA256SUMS"
        original_checksums = checksum_file.read_bytes()
        final_log = item_directory / "logs" / self.variant.log_name
        original_log = final_log.read_bytes() if final_log.is_file() else None
        output_root = item_directory / self.variant.output_subpath
        staging = output_root / f".staging-{job_id}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        published: Path | None = None
        try:
            if plan.segmented:
                staged_segments = staging / "segments"
                staged_segments.mkdir()
                for source in generated:
                    shutil.copy2(source, staged_segments / source.name)
                final_segments = output_root / "segments"
                os.replace(staged_segments, final_segments)
                published = final_segments
            else:
                staged_file = staging / generated[0].name
                shutil.copy2(generated[0], staged_file)
                final_file = output_root / generated[0].name
                os.replace(staged_file, final_file)
                published = final_file
            final_log.parent.mkdir(parents=True, exist_ok=True)
            staged_log = staging / "convert.log"
            shutil.copy2(convert_log, staged_log)
            os.replace(staged_log, final_log)

            section = manifest[self.variant.manifest_section]
            if not isinstance(section, list):
                raise ExistingDerivativeConflict(
                    f"Manifest {self.variant.manifest_section} field is invalid"
                )
            manifest[self.variant.manifest_section] = section + [
                asset.manifest_record(source_sha256) for asset in assets
            ]
            write_manifest_atomic(manifest_path, manifest)
            new_paths = [Path(asset.relative_path) for asset in assets]
            new_paths.extend(
                (Path("logs") / self.variant.log_name, Path("metadata/archive.json"))
            )
            write_sha256sums(item_directory, list(dict.fromkeys(checksum_paths + new_paths)))
            verified = verify_sha256sums(item_directory)
            if not verified.valid:
                raise ExistingDerivativeConflict(
                    f"Published {self.variant.label} output failed integrity verification: "
                    + "; ".join(verified.errors)
                )
        except Exception:
            _restore_bytes(manifest_path, original_manifest)
            _restore_bytes(checksum_file, original_checksums)
            if published:
                if published.is_dir():
                    shutil.rmtree(published, ignore_errors=True)
                elif published.exists():
                    published.unlink()
            if original_log is None:
                final_log.unlink(missing_ok=True)
            else:
                _restore_bytes(final_log, original_log)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return PcmResult(
            str(manifest["archive_id"]),
            item_directory,
            assets,
            plan.segmented,
            False,
        )


class AbletonService(PcmWavService):
    """The default editing target: 32-bit float PCM, fixed by DEC-008."""

    def __init__(self, config: AppConfig, runner: CommandRunner):
        super().__init__(config, runner, ABLETON_VARIANT)
