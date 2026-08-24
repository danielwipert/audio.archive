from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path

from ..urls import parse_youtube_url
from .db import CloudDatabase
from .models import CloudJobRequest, CloudProfile


CSV_COLUMNS = {"artist", "title", "version", "url", "profile"}
_PROFILE_ALIASES = {
    "ableton": CloudProfile.ABLETON,
    "source": CloudProfile.SOURCE,
    "archive": CloudProfile.SOURCE,
    "package": CloudProfile.PACKAGE,
    "complete": CloudProfile.PACKAGE,
}


@dataclass(frozen=True)
class CloudCsvRejectedRow:
    row_number: int
    message: str


@dataclass(frozen=True)
class CloudCsvPreview:
    filename: str
    file_sha256: str
    accepted: tuple[CloudJobRequest, ...]
    rejected: tuple[CloudCsvRejectedRow, ...]
    duplicate_rows: tuple[int, ...]


@dataclass(frozen=True)
class CloudCsvImportResult:
    import_id: int
    job_ids: tuple[int, ...]
    preview: CloudCsvPreview


def preview_cloud_csv(
    *,
    filename: str,
    content: bytes,
    max_bytes: int = 5 * 1024 * 1024,
) -> CloudCsvPreview:
    safe_name = Path(filename or "import.csv").name
    if Path(safe_name).suffix.casefold() != ".csv":
        raise ValueError("Only CSV files are accepted")
    if len(content) > max_bytes:
        raise ValueError(f"CSV exceeds the configured {max_bytes}-byte limit")
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

    accepted: list[CloudJobRequest] = []
    rejected: list[CloudCsvRejectedRow] = []
    duplicates: list[int] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    for row_number, raw_row in enumerate(reader, start=2):
        row = {(key or "").strip().casefold(): value for key, value in raw_row.items()}
        if not any(_clean(value) for value in row.values()):
            continue
        try:
            request = _normalize_cloud_request(
                artist=row.get("artist"),
                title=row.get("title"),
                version=row.get("version"),
                url=row.get("url"),
                profile=row.get("profile"),
                import_row=row_number,
            )
        except ValueError as exc:
            rejected.append(CloudCsvRejectedRow(row_number, str(exc)))
            continue
        key = tuple(
            (value or "").casefold()
            for value in (
                request.artist,
                request.title,
                request.version,
                request.url,
                request.profile.value,
            )
        )
        if key in seen:
            duplicates.append(row_number)
            continue
        seen.add(key)
        accepted.append(request)

    return CloudCsvPreview(
        filename=safe_name,
        file_sha256=digest,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        duplicate_rows=tuple(duplicates),
    )


def import_cloud_csv(database: CloudDatabase, preview: CloudCsvPreview) -> CloudCsvImportResult:
    with database.connect() as connection:
        row = connection.execute(
            """
            INSERT INTO csv_imports (
                filename, file_sha256, accepted_rows, rejected_rows, duplicate_rows
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                preview.filename,
                preview.file_sha256,
                len(preview.accepted),
                len(preview.rejected),
                len(preview.duplicate_rows),
            ),
        ).fetchone()
        assert row is not None
        import_id = int(row["id"])

    job_ids = tuple(
        database.create_job(replace(request, import_id=import_id))
        for request in preview.accepted
    )
    return CloudCsvImportResult(import_id=import_id, job_ids=job_ids, preview=preview)


def _normalize_cloud_request(
    *,
    artist: str | None,
    title: str | None,
    version: str | None,
    url: str | None,
    profile: str | None,
    import_row: int,
) -> CloudJobRequest:
    clean_url = _clean(url)
    canonical_url = parse_youtube_url(clean_url).canonical_url if clean_url else None
    profile_name = (_clean(profile) or CloudProfile.ABLETON.value).casefold()
    try:
        cloud_profile = _PROFILE_ALIASES[profile_name]
    except KeyError as exc:
        raise ValueError(
            "Unknown output profile: "
            f"{profile_name}. Use ableton, source, or package."
        ) from exc
    request = CloudJobRequest(
        artist=_clean(artist),
        title=_clean(title),
        version=_clean(version),
        url=canonical_url,
        profile=cloud_profile,
        origin="csv",
        import_row=import_row,
    )
    request.validate()
    return request


def _clean(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None
