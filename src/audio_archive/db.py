from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import ACTIVE_STATES, JobRequest, JobState, ensure_transition
from .urls import parse_youtube_url

SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ArchiveDatabase:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema {current} is newer than supported schema {SCHEMA_VERSION}"
                )
            if current == 0:
                connection.executescript(
                    """
                    CREATE TABLE csv_imports (
                        id INTEGER PRIMARY KEY,
                        filename TEXT NOT NULL,
                        file_sha256 TEXT NOT NULL,
                        imported_at_utc TEXT NOT NULL,
                        accepted_rows INTEGER NOT NULL,
                        rejected_rows INTEGER NOT NULL,
                        duplicate_rows INTEGER NOT NULL,
                        UNIQUE(file_sha256, imported_at_utc)
                    );

                    CREATE TABLE jobs (
                        id INTEGER PRIMARY KEY,
                        state TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        requested_artist TEXT,
                        requested_title TEXT,
                        requested_version TEXT,
                        requested_url TEXT,
                        profile TEXT NOT NULL,
                        import_id INTEGER REFERENCES csv_imports(id),
                        import_row INTEGER,
                        source_extractor TEXT,
                        source_id TEXT,
                        source_url TEXT,
                        source_title TEXT,
                        source_creator TEXT,
                        resolution_method TEXT,
                        selected_score INTEGER,
                        runner_up_score INTEGER,
                        progress_percent REAL,
                        quality_status TEXT,
                        warning_summary TEXT,
                        error_stage TEXT,
                        error_summary TEXT,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        created_at_utc TEXT NOT NULL,
                        updated_at_utc TEXT NOT NULL,
                        started_at_utc TEXT,
                        completed_at_utc TEXT
                    );
                    CREATE INDEX jobs_state_idx ON jobs(state);
                    CREATE INDEX jobs_source_idx ON jobs(source_extractor, source_id);

                    CREATE TABLE job_events (
                        id INTEGER PRIMARY KEY,
                        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        occurred_at_utc TEXT NOT NULL,
                        from_state TEXT,
                        to_state TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        message TEXT,
                        detail_json TEXT
                    );
                    CREATE INDEX job_events_job_idx ON job_events(job_id, id);

                    CREATE TABLE candidates (
                        id INTEGER PRIMARY KEY,
                        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        video_id TEXT NOT NULL,
                        url TEXT NOT NULL,
                        title TEXT NOT NULL,
                        channel TEXT,
                        duration_seconds REAL,
                        thumbnail_url TEXT,
                        score INTEGER NOT NULL,
                        reasons_json TEXT NOT NULL,
                        warnings_json TEXT NOT NULL,
                        disqualified INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(job_id, video_id)
                    );

                    CREATE TABLE archive_items (
                        archive_id TEXT PRIMARY KEY,
                        extractor TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        item_directory TEXT NOT NULL,
                        manifest_path TEXT NOT NULL,
                        quality_status TEXT NOT NULL,
                        source_master_sha256 TEXT NOT NULL,
                        verified_at_utc TEXT NOT NULL,
                        UNIQUE(extractor, source_id)
                    );

                    CREATE TABLE assets (
                        id INTEGER PRIMARY KEY,
                        archive_id TEXT NOT NULL REFERENCES archive_items(archive_id) ON DELETE CASCADE,
                        role TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        media_properties_json TEXT NOT NULL,
                        verified_at_utc TEXT NOT NULL,
                        UNIQUE(archive_id, role, relative_path)
                    );

                    CREATE TABLE worker_claims (
                        job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                        claim_token TEXT NOT NULL UNIQUE,
                        claimed_at_utc TEXT NOT NULL
                    );
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif current == 1:
                connection.execute(
                    """
                    CREATE TABLE worker_claims (
                        job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                        claim_token TEXT NOT NULL UNIQUE,
                        claimed_at_utc TEXT NOT NULL
                    )
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def create_csv_import(
        self,
        *,
        filename: str,
        file_sha256: str,
        accepted_rows: int,
        rejected_rows: int,
        duplicate_rows: int,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO csv_imports (
                    filename, file_sha256, imported_at_utc,
                    accepted_rows, rejected_rows, duplicate_rows
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    filename,
                    file_sha256,
                    utc_now(),
                    accepted_rows,
                    rejected_rows,
                    duplicate_rows,
                ),
            )
            return int(cursor.lastrowid)

    def create_job(self, request: JobRequest) -> int:
        request.validate()
        now = utc_now()
        state = JobState.READY if request.url else JobState.PENDING
        pinned_source = parse_youtube_url(request.url) if request.url else None
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    state, origin, requested_artist, requested_title, requested_version,
                    requested_url, profile, import_id, import_row,
                    source_extractor, source_id, source_url, resolution_method,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.value,
                    request.origin,
                    request.artist,
                    request.title,
                    request.version,
                    request.url,
                    request.profile,
                    request.import_id,
                    request.import_row,
                    "youtube" if pinned_source else None,
                    pinned_source.video_id if pinned_source else None,
                    pinned_source.canonical_url if pinned_source else None,
                    "exact_url" if pinned_source else None,
                    now,
                    now,
                ),
            )
            job_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, occurred_at_utc, from_state, to_state, event_type, message
                ) VALUES (?, ?, NULL, ?, 'created', ?)
                """,
                (job_id, now, state.value, f"Created from {request.origin} input"),
            )
            return job_id

    def get_job(self, job_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT jobs.*,
                       csv_imports.filename AS import_filename,
                       csv_imports.file_sha256 AS import_file_sha256
                FROM jobs
                LEFT JOIN csv_imports ON csv_imports.id = jobs.import_id
                WHERE jobs.id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Job {job_id} does not exist")
        return row

    def list_jobs(self, state: JobState | None = None) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if state:
                return list(
                    connection.execute(
                        "SELECT * FROM jobs WHERE state = ? ORDER BY id", (state.value,)
                    )
                )
            return list(connection.execute("SELECT * FROM jobs ORDER BY id"))

    def list_job_events(self, job_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM job_events WHERE job_id = ? ORDER BY id",
                    (job_id,),
                )
            )

    def claim_next_runnable_job(self, claim_token: str) -> int | None:
        if not claim_token:
            raise ValueError("Worker claim token is required")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM worker_claims LIMIT 1").fetchone():
                return None
            job = connection.execute(
                """
                SELECT id, state
                FROM jobs
                WHERE state IN ('ready', 'converting')
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
            if job is None:
                return None
            connection.execute(
                "INSERT INTO worker_claims (job_id, claim_token, claimed_at_utc) VALUES (?, ?, ?)",
                (job["id"], claim_token, now),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, occurred_at_utc, from_state, to_state,
                    event_type, message, detail_json
                ) VALUES (?, ?, ?, ?, 'worker_claimed', ?, ?)
                """,
                (
                    job["id"],
                    now,
                    job["state"],
                    job["state"],
                    "Sequential worker claimed job",
                    json.dumps({"claim_token": claim_token}, sort_keys=True),
                ),
            )
            return int(job["id"])

    def release_worker_claim(self, job_id: int, claim_token: str) -> bool:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT claim_token FROM worker_claims WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None or row["claim_token"] != claim_token:
                return False
            connection.execute("DELETE FROM worker_claims WHERE job_id = ?", (job_id,))
            state = connection.execute(
                "SELECT state FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()["state"]
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, occurred_at_utc, from_state, to_state,
                    event_type, message
                ) VALUES (?, ?, ?, ?, 'worker_released', ?)
                """,
                (job_id, now, state, state, "Sequential worker released job"),
            )
            return True

    def clear_worker_claims(self) -> int:
        with self.connect() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM worker_claims").fetchone()[0])
            connection.execute("DELETE FROM worker_claims")
            return count

    def list_worker_claims(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT job_id, claim_token, claimed_at_utc FROM worker_claims ORDER BY job_id"
                )
            )

    def recover_interrupted_jobs(self) -> int:
        now = utc_now()
        with self.connect() as connection:
            rows = list(
                connection.execute(
                    "SELECT id, source_extractor, source_id, source_url FROM jobs "
                    "WHERE state = 'interrupted' ORDER BY id"
                )
            )
            for row in rows:
                target = (
                    JobState.READY
                    if row["source_extractor"] == "youtube"
                    and row["source_id"]
                    and row["source_url"]
                    else JobState.PENDING
                )
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = ?, updated_at_utc = ?, error_stage = NULL, error_summary = NULL
                    WHERE id = ?
                    """,
                    (target.value, now, row["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, occurred_at_utc, from_state, to_state,
                        event_type, message
                    ) VALUES (?, ?, 'interrupted', ?, 'recovery', ?)
                    """,
                    (
                        row["id"],
                        now,
                        target.value,
                        "Interrupted job requeued from its last durable boundary",
                    ),
                )
            return len(rows)

    def retry_job(self, job_id: int) -> JobState:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT state, source_extractor, source_id, source_url FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            old_state = JobState(row["state"])
            if old_state not in {JobState.FAILED, JobState.INTERRUPTED}:
                raise ValueError(f"Job {job_id} is not failed or interrupted")
            target = (
                JobState.READY
                if row["source_extractor"] == "youtube"
                and row["source_id"]
                and row["source_url"]
                else JobState.PENDING
            )
            ensure_transition(old_state, target)
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, retry_count = retry_count + 1,
                    error_stage = NULL, error_summary = NULL,
                    progress_percent = NULL, completed_at_utc = NULL,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (target.value, now, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, occurred_at_utc, from_state, to_state,
                    event_type, message
                ) VALUES (?, ?, ?, ?, 'retry', ?)
                """,
                (job_id, now, old_state.value, target.value, "Job queued for retry"),
            )
            return target

    def find_archive_item(self, extractor: str, source_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM archive_items WHERE extractor = ? AND source_id = ?",
                (extractor, source_id),
            ).fetchone()

    def list_assets(self, archive_id: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM assets WHERE archive_id = ? ORDER BY role, relative_path",
                    (archive_id,),
                )
            )

    def record_acquisition(
        self,
        job_id: int,
        *,
        archive_id: str,
        source_id: str,
        source_title: str,
        source_creator: str | None,
        item_directory: str,
        manifest_path: str,
        quality_status: str,
        master_relative_path: str,
        master_sha256: str,
        media_properties: dict[str, object],
        warnings: list[str],
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET source_title = ?, source_creator = ?, quality_status = ?,
                    warning_summary = ?, progress_percent = 100, updated_at_utc = ?
                WHERE id = ?
                """,
                (
                    source_title,
                    source_creator,
                    quality_status,
                    " | ".join(warnings) if warnings else None,
                    now,
                    job_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO archive_items (
                    archive_id, extractor, source_id, item_directory, manifest_path,
                    quality_status, source_master_sha256, verified_at_utc
                ) VALUES (?, 'youtube', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(archive_id) DO UPDATE SET
                    item_directory = excluded.item_directory,
                    manifest_path = excluded.manifest_path,
                    quality_status = excluded.quality_status,
                    source_master_sha256 = excluded.source_master_sha256,
                    verified_at_utc = excluded.verified_at_utc
                """,
                (
                    archive_id,
                    source_id,
                    item_directory,
                    manifest_path,
                    quality_status,
                    master_sha256,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO assets (
                    archive_id, role, relative_path, sha256,
                    media_properties_json, verified_at_utc
                ) VALUES (?, 'source_master', ?, ?, ?, ?)
                ON CONFLICT(archive_id, role, relative_path) DO UPDATE SET
                    sha256 = excluded.sha256,
                    media_properties_json = excluded.media_properties_json,
                    verified_at_utc = excluded.verified_at_utc
                """,
                (
                    archive_id,
                    master_relative_path,
                    master_sha256,
                    json.dumps(media_properties, sort_keys=True),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, occurred_at_utc, from_state, to_state,
                    event_type, message, detail_json
                ) VALUES (?, ?, 'verifying_master', 'verifying_master',
                          'acquisition_recorded', ?, ?)
                """,
                (
                    job_id,
                    now,
                    f"Recorded verified source master {master_relative_path}",
                    json.dumps(
                        {
                            "archive_id": archive_id,
                            "quality_status": quality_status,
                            "sha256": master_sha256,
                        },
                        sort_keys=True,
                    ),
                ),
            )

    def record_ableton_outputs(
        self,
        job_id: int,
        *,
        archive_id: str,
        assets: list[dict[str, object]],
        reused_existing: bool,
    ) -> None:
        if not assets:
            raise ValueError("At least one Ableton asset is required")
        now = utc_now()
        with self.connect() as connection:
            job = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            if JobState(job["state"]) != JobState.CONVERTING:
                raise ValueError(f"Job {job_id} must be converting to record Ableton outputs")
            for asset in assets:
                relative_path = str(asset["relative_path"])
                sha256 = str(asset["sha256"])
                media_properties = dict(asset["media_properties"])
                connection.execute(
                    """
                    INSERT INTO assets (
                        archive_id, role, relative_path, sha256,
                        media_properties_json, verified_at_utc
                    ) VALUES (?, 'ableton', ?, ?, ?, ?)
                    ON CONFLICT(archive_id, role, relative_path) DO UPDATE SET
                        sha256 = excluded.sha256,
                        media_properties_json = excluded.media_properties_json,
                        verified_at_utc = excluded.verified_at_utc
                    """,
                    (
                        archive_id,
                        relative_path,
                        sha256,
                        json.dumps(media_properties, sort_keys=True),
                        now,
                    ),
                )
            connection.execute(
                "UPDATE jobs SET progress_percent = 90, updated_at_utc = ? WHERE id = ?",
                (now, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, occurred_at_utc, from_state, to_state,
                    event_type, message, detail_json
                ) VALUES (?, ?, 'converting', 'converting',
                          'ableton_outputs_recorded', ?, ?)
                """,
                (
                    job_id,
                    now,
                    f"Recorded {len(assets)} verified Ableton output(s)",
                    json.dumps(
                        {
                            "archive_id": archive_id,
                            "paths": [str(asset["relative_path"]) for asset in assets],
                            "reused_existing": reused_existing,
                        },
                        sort_keys=True,
                    ),
                ),
            )

    def record_listening_output(
        self,
        job_id: int,
        *,
        archive_id: str,
        relative_path: str,
        sha256: str,
        media_properties: dict[str, object],
        reused_existing: bool,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            job = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")
            if JobState(job["state"]) != JobState.CONVERTING:
                raise ValueError(f"Job {job_id} must be converting to record listening output")
            connection.execute(
                """
                INSERT INTO assets (
                    archive_id, role, relative_path, sha256,
                    media_properties_json, verified_at_utc
                ) VALUES (?, 'listening', ?, ?, ?, ?)
                ON CONFLICT(archive_id, role, relative_path) DO UPDATE SET
                    sha256 = excluded.sha256,
                    media_properties_json = excluded.media_properties_json,
                    verified_at_utc = excluded.verified_at_utc
                """,
                (
                    archive_id,
                    relative_path,
                    sha256,
                    json.dumps(media_properties, sort_keys=True),
                    now,
                ),
            )
            connection.execute(
                "UPDATE jobs SET progress_percent = 95, updated_at_utc = ? WHERE id = ?",
                (now, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, occurred_at_utc, from_state, to_state,
                    event_type, message, detail_json
                ) VALUES (?, ?, 'converting', 'converting',
                          'listening_output_recorded', ?, ?)
                """,
                (
                    job_id,
                    now,
                    f"Recorded verified listening output {relative_path}",
                    json.dumps(
                        {
                            "archive_id": archive_id,
                            "path": relative_path,
                            "reused_existing": reused_existing,
                        },
                        sort_keys=True,
                    ),
                ),
            )

    def fail_job(self, job_id: int, *, stage: str, summary: str) -> None:
        with self.connect() as connection:
            row = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            old_state = JobState(row["state"])
            ensure_transition(old_state, JobState.FAILED)
            now = utc_now()
            connection.execute(
                """
                UPDATE jobs
                SET state = 'failed', error_stage = ?, error_summary = ?,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (stage, summary[:2000], now, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, occurred_at_utc, from_state, to_state,
                    event_type, message
                ) VALUES (?, ?, ?, 'failed', 'stage_failure', ?)
                """,
                (job_id, now, old_state.value, summary[:2000]),
            )

    def transition_job(
        self,
        job_id: int,
        new_state: JobState,
        *,
        message: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT state FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} does not exist")
            old_state = JobState(row["state"])
            ensure_transition(old_state, new_state)
            now = utc_now()
            completed_at = now if new_state in {
                JobState.COMPLETED,
                JobState.COMPLETED_WITH_WARNINGS,
                JobState.SKIPPED_DUPLICATE,
                JobState.NOT_FOUND,
                JobState.CANCELLED,
            } else None
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, updated_at_utc = ?,
                    progress_percent = CASE
                        WHEN ? IN (
                            'completed', 'completed_with_warnings', 'skipped_duplicate',
                            'not_found', 'cancelled'
                        ) THEN 100
                        ELSE progress_percent
                    END,
                    started_at_utc = CASE
                        WHEN started_at_utc IS NULL AND ? IN ('resolving', 'downloading') THEN ?
                        ELSE started_at_utc
                    END,
                    completed_at_utc = COALESCE(?, completed_at_utc)
                WHERE id = ?
                """,
                (
                    new_state.value,
                    now,
                    new_state.value,
                    new_state.value,
                    now,
                    completed_at,
                    job_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, occurred_at_utc, from_state, to_state,
                    event_type, message, detail_json
                ) VALUES (?, ?, ?, ?, 'state_transition', ?, ?)
                """,
                (
                    job_id,
                    now,
                    old_state.value,
                    new_state.value,
                    message,
                    json.dumps(detail, sort_keys=True) if detail else None,
                ),
            )

    def interrupt_active_jobs(self) -> int:
        active_values = tuple(state.value for state in ACTIVE_STATES)
        placeholders = ",".join("?" for _ in active_values)
        now = utc_now()
        with self.connect() as connection:
            rows = list(
                connection.execute(
                    f"SELECT id, state FROM jobs WHERE state IN ({placeholders})", active_values
                )
            )
            for row in rows:
                connection.execute(
                    "UPDATE jobs SET state = ?, updated_at_utc = ? WHERE id = ?",
                    (JobState.INTERRUPTED.value, now, row["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, occurred_at_utc, from_state, to_state, event_type, message
                    ) VALUES (?, ?, ?, ?, 'recovery', ?)
                    """,
                    (
                        row["id"],
                        now,
                        row["state"],
                        JobState.INTERRUPTED.value,
                        "Active job found during startup recovery",
                    ),
                )
            return len(rows)
