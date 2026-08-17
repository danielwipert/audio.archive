from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .verify import sha256_file


@dataclass(frozen=True)
class IntegrityResult:
    valid: bool
    checked_files: int
    errors: tuple[str, ...]


def write_sha256sums(item_root: Path, relative_paths: list[Path]) -> Path:
    normalized: list[tuple[str, str]] = []
    for relative in relative_paths:
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Checksum path must remain beneath the item root: {relative}")
        full_path = item_root / relative
        if not full_path.is_file():
            raise FileNotFoundError(full_path)
        normalized.append((relative.as_posix(), sha256_file(full_path)))
    output = item_root / "checksums" / "SHA256SUMS"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(
        "".join(f"{digest}  {path}\n" for path, digest in sorted(normalized)),
        encoding="utf-8",
    )
    temporary.replace(output)
    return output


def listed_checksum_paths(item_root: Path) -> list[Path]:
    checksum_file = item_root / "checksums" / "SHA256SUMS"
    if not checksum_file.is_file():
        raise FileNotFoundError(checksum_file)
    paths: list[Path] = []
    for line_number, line in enumerate(checksum_file.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            _, relative_text = line.split("  ", 1)
        except ValueError as exc:
            raise ValueError(f"invalid checksum line {line_number}") from exc
        relative = PurePosixPath(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe checksum path on line {line_number}")
        paths.append(Path(*relative.parts))
    if not paths:
        raise ValueError("SHA256SUMS does not list any files")
    return paths


def verify_sha256sums(item_root: Path) -> IntegrityResult:
    checksum_file = item_root / "checksums" / "SHA256SUMS"
    if not checksum_file.is_file():
        return IntegrityResult(False, 0, ("checksums/SHA256SUMS is missing",))
    errors: list[str] = []
    checked = 0
    for line_number, line in enumerate(checksum_file.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            expected, relative_text = line.split("  ", 1)
        except ValueError:
            errors.append(f"invalid checksum line {line_number}")
            continue
        relative = PurePosixPath(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"unsafe checksum path on line {line_number}")
            continue
        full_path = item_root.joinpath(*relative.parts)
        if not full_path.is_file():
            errors.append(f"missing file: {relative_text}")
            continue
        actual = sha256_file(full_path)
        checked += 1
        if actual != expected:
            errors.append(f"checksum mismatch: {relative_text}")
    return IntegrityResult(not errors and checked > 0, checked, tuple(errors))
