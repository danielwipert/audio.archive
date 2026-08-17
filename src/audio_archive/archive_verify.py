from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .integrity import listed_checksum_paths, verify_sha256sums
from .manifest import validate_manifest
from .verify import sha256_file


@dataclass(frozen=True)
class ArchiveVerification:
    item_directory: Path
    archive_id: str | None
    valid: bool
    checked_files: int
    errors: tuple[str, ...]


def _asset_records(manifest: dict[str, object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    source_master = manifest.get("source_master")
    if isinstance(source_master, dict):
        records.append(source_master)
    for field in ("intermediates", "derivatives"):
        values = manifest.get(field)
        if isinstance(values, list):
            records.extend(value for value in values if isinstance(value, dict))
    return records


def verify_archive_item(item_directory: Path) -> ArchiveVerification:
    errors: list[str] = []
    integrity = verify_sha256sums(item_directory)
    errors.extend(integrity.errors)
    manifest_path = item_directory / "metadata" / "archive.json"
    archive_id: str | None = None
    manifest: dict[str, object] | None = None
    if not manifest_path.is_file():
        errors.append("metadata/archive.json is missing")
    else:
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            errors.append("metadata/archive.json is invalid JSON")
        else:
            if not isinstance(loaded, dict):
                errors.append("metadata/archive.json must contain an object")
            else:
                manifest = loaded
                archive_id = str(manifest.get("archive_id") or "") or None
                validation = validate_manifest(manifest)
                errors.extend(validation.errors)

    try:
        checksum_paths = listed_checksum_paths(item_directory)
    except (FileNotFoundError, ValueError) as exc:
        errors.append(str(exc))
        checksum_paths = []
    listed = {path.as_posix() for path in checksum_paths}
    if "metadata/archive.json" not in listed:
        errors.append("metadata/archive.json is absent from SHA256SUMS")

    if manifest:
        seen: set[str] = set()
        for record in _asset_records(manifest):
            role = str(record.get("role") or "asset")
            relative_text = str(record.get("path") or "")
            relative = PurePosixPath(relative_text)
            if not relative_text or relative.is_absolute() or ".." in relative.parts:
                errors.append(f"{role} has an unsafe path: {relative_text!r}")
                continue
            if relative_text in seen:
                errors.append(f"duplicate manifest asset path: {relative_text}")
                continue
            seen.add(relative_text)
            if relative_text not in listed:
                errors.append(f"manifest asset is absent from SHA256SUMS: {relative_text}")
            full_path = item_directory.joinpath(*relative.parts)
            if not full_path.is_file():
                errors.append(f"manifest asset is missing: {relative_text}")
                continue
            expected = str(record.get("sha256") or "")
            if not expected or sha256_file(full_path) != expected:
                errors.append(f"manifest checksum mismatch: {relative_text}")

    return ArchiveVerification(
        item_directory=item_directory,
        archive_id=archive_id,
        valid=not errors,
        checked_files=integrity.checked_files,
        errors=tuple(dict.fromkeys(errors)),
    )
