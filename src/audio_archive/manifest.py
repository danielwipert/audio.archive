from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
from typing import Any


SCHEMA_VERSION = "1.2"


REQUIRED_TOP_LEVEL_KEYS = {
    "schema_version",
    "archive_id",
    "content_type",
    "request",
    "resolution",
    "source",
    "acquisition",
    "source_master",
    "intermediates",
    "derivatives",
}


@dataclass(frozen=True)
class ManifestValidation:
    valid: bool
    errors: tuple[str, ...]


def validate_manifest(data: dict[str, Any]) -> ManifestValidation:
    errors: list[str] = []
    missing = REQUIRED_TOP_LEVEL_KEYS - data.keys()
    if missing:
        errors.append(f"missing top-level fields: {', '.join(sorted(missing))}")
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    archive_id = data.get("archive_id")
    if not isinstance(archive_id, str) or ":" not in archive_id:
        errors.append("archive_id must be a namespaced string")
    if not isinstance(data.get("intermediates"), list):
        errors.append("intermediates must be a list")
    if not isinstance(data.get("derivatives"), list):
        errors.append("derivatives must be a list")
    return ManifestValidation(not errors, tuple(errors))


def write_manifest_atomic(path: Path, data: dict[str, Any]) -> None:
    result = validate_manifest(data)
    if not result.valid:
        raise ValueError("Invalid archive manifest: " + "; ".join(result.errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)

