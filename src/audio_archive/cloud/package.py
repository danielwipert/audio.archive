from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

from ..integrity import verify_sha256sums
from ..verify import sha256_file


@dataclass(frozen=True)
class ArchivePackage:
    path: Path
    sha256: str
    size_bytes: int


def build_archive_package(item_directory: Path, output_path: Path) -> ArchivePackage:
    """Create a no-reencode ZIP handoff from an already verified job archive tree."""
    manifest = item_directory / "metadata" / "archive.json"
    checksums = item_directory / "checksums" / "SHA256SUMS"
    if not manifest.is_file() or not checksums.is_file():
        raise ValueError("Archive package requires verified metadata and SHA256SUMS")
    integrity = verify_sha256sums(item_directory)
    if not integrity.valid:
        raise ValueError(
            "Archive package source tree failed integrity verification: "
            + "; ".join(integrity.errors)
        )

    files = sorted(path for path in item_directory.rglob("*") if path.is_file())
    if not files:
        raise ValueError("Archive package has no files")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    root_name = f"{item_directory.name}-archive"
    try:
        with ZipFile(temporary, "w", compression=ZIP_STORED, allowZip64=True) as archive:
            for path in files:
                relative = path.relative_to(item_directory)
                archive.write(path, arcname=(Path(root_name) / relative).as_posix())
        with ZipFile(temporary, "r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ValueError(f"Archive package failed ZIP integrity check: {bad_member}")
        temporary.replace(output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return ArchivePackage(
        path=output_path,
        sha256=sha256_file(output_path),
        size_bytes=output_path.stat().st_size,
    )
