from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from .ableton import AbletonResult, AbletonService
from .config import AppConfig, load_config
from .integrity import write_sha256sums
from .manifest import write_manifest_atomic
from .tooling import CommandRunner, SubprocessRunner, resolve_tool
from .verify import sha256_file


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _generate_source(config: AppConfig, runner: CommandRunner, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = resolve_tool(config.ffmpeg, config.tools_directory)
    runner.run(
        (
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3.3",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(path),
        )
    )


def _prepare_item(
    item_directory: Path,
    *,
    source_id: str,
    source_fixture: Path,
) -> None:
    master_relative = Path("master") / f"{source_id}.wav"
    master = item_directory / master_relative
    master.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_fixture, master)
    digest = sha256_file(master)
    manifest = {
        "schema_version": "1.2",
        "archive_id": f"acceptance:{source_id}",
        "content_type": "acceptance_fixture",
        "request": {"origin": "acceptance", "profile": "ableton"},
        "resolution": {"method": "local_fixture"},
        "source": {
            "platform": "local_acceptance_fixture",
            "id": source_id,
            "url": None,
            "title": "Audio Archive Ableton acceptance tone",
            "creator": "Audio Archive",
            "duration_seconds": 3.3,
        },
        "acquisition": {"quality_status": "local_fixture"},
        "source_master": {
            "role": "source_master",
            "path": master_relative.as_posix(),
            "sha256": digest,
            "container": "wav",
            "audio_codec": "pcm_s16le",
            "sample_rate_hz": 48000,
            "channels": 2,
        },
        "intermediates": [],
        "derivatives": [],
    }
    manifest_path = item_directory / "metadata" / "archive.json"
    write_manifest_atomic(manifest_path, manifest)
    write_sha256sums(item_directory, [master_relative, Path("metadata/archive.json")])


def _result_payload(result: AbletonResult) -> dict[str, object]:
    return {
        "segmented": result.segmented,
        "item_directory": str(result.item_directory),
        "paths": [str(asset.path) for asset in result.assets],
        "sample_rate_hz": result.assets[0].sample_rate_hz,
        "channels": result.assets[0].channels,
        "segments": len(result.assets),
    }


def create_ableton_acceptance_fixtures(
    config: AppConfig,
    runner: CommandRunner,
) -> dict[str, object]:
    root = config.archive_root / "acceptance" / f"ableton-{_timestamp()}"
    source_fixture = root / "source" / "acceptance-tone.wav"
    _generate_source(config, runner, source_fixture)

    normal_item = root / "normal"
    segmented_item = root / "segmented"
    _prepare_item(normal_item, source_id="NORMALTEST01", source_fixture=source_fixture)
    _prepare_item(segmented_item, source_id="SEGMENTTEST", source_fixture=source_fixture)

    normal = AbletonService(config, runner).create(normal_item, job_id=900001)
    segmented_config = replace(config, safe_wav_size_gib=0.001)
    segmented = AbletonService(segmented_config, runner).create(segmented_item, job_id=900002)

    if normal.segmented:
        raise RuntimeError("Normal Ableton acceptance fixture unexpectedly segmented")
    if not segmented.segmented or len(segmented.assets) < 2:
        raise RuntimeError("Segmented Ableton acceptance fixture did not create multiple files")

    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "normal": _result_payload(normal),
        "segmented": _result_payload(segmented),
        "note": (
            "The segmented fixture uses an acceptance-only 0.001 GiB threshold to exercise "
            "the same production segmentation code path without generating a multi-gigabyte file."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate persistent Ableton acceptance fixtures")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args(argv)
    result = create_ableton_acceptance_fixtures(load_config(), SubprocessRunner())
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Ableton acceptance fixtures created:")
        print(f"  Normal: {result['normal']['paths'][0]}")
        print(f"  Segmented: {result['segmented']['item_directory']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
