from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
import csv
import io

from .models import JobRequest
from .urls import parse_youtube_url


CSV_COLUMNS = {"artist", "title", "version", "url", "profile"}


@dataclass(frozen=True)
class RejectedRow:
    row_number: int
    message: str


@dataclass(frozen=True)
class CsvPreview:
    filename: str
    file_sha256: str
    accepted: tuple[JobRequest, ...]
    rejected: tuple[RejectedRow, ...]
    duplicate_rows: tuple[int, ...]


def _clean(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def normalize_request(
    *,
    artist: str | None = None,
    title: str | None = None,
    version: str | None = None,
    url: str | None = None,
    profile: str | None = None,
    origin: str = "manual",
    import_row: int | None = None,
) -> JobRequest:
    clean_url = _clean(url)
    canonical_url = parse_youtube_url(clean_url).canonical_url if clean_url else None
    request = JobRequest(
        artist=_clean(artist),
        title=_clean(title),
        version=_clean(version),
        url=canonical_url,
        profile=(_clean(profile) or "ableton").casefold(),
        origin=origin,
        import_row=import_row,
    )
    request.validate()
    return request


def preview_csv(path: Path, *, max_bytes: int = 5 * 1024 * 1024) -> CsvPreview:
    if path.suffix.casefold() != ".csv":
        raise ValueError("Only CSV files are accepted")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"CSV exceeds the configured {max_bytes}-byte limit")
    content = path.read_bytes()
    digest = sha256(content).hexdigest()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded") from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise ValueError("CSV has no header row")
    headers = [(header or "").strip().casefold() for header in reader.fieldnames]
    if len(headers) != len(set(headers)):
        raise ValueError("CSV contains duplicate headers")
    unknown = set(headers) - CSV_COLUMNS
    if unknown:
        raise ValueError(f"Unsupported CSV column(s): {', '.join(sorted(unknown))}")
    if "url" not in headers and not {"artist", "title"}.issubset(headers):
        raise ValueError("CSV requires artist and title columns unless it has a URL column")

    accepted: list[JobRequest] = []
    rejected: list[RejectedRow] = []
    duplicates: list[int] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    for row_number, raw_row in enumerate(reader, start=2):
        row = {(key or "").strip().casefold(): value for key, value in raw_row.items()}
        if not any(_clean(value) for value in row.values()):
            continue
        try:
            request = normalize_request(
                artist=row.get("artist"),
                title=row.get("title"),
                version=row.get("version"),
                url=row.get("url"),
                profile=row.get("profile"),
                origin="csv",
                import_row=row_number,
            )
        except ValueError as exc:
            rejected.append(RejectedRow(row_number, str(exc)))
            continue
        key = tuple(
            (value or "").casefold()
            for value in (
                request.artist,
                request.title,
                request.version,
                request.url,
                request.profile,
            )
        )
        if key in seen:
            duplicates.append(row_number)
            continue
        seen.add(key)
        accepted.append(request)

    return CsvPreview(
        filename=path.name,
        file_sha256=digest,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        duplicate_rows=tuple(duplicates),
    )


def attach_import_id(request: JobRequest, import_id: int) -> JobRequest:
    return replace(request, import_id=import_id)

