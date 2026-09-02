from __future__ import annotations

from dataclasses import dataclass
import json

from .config import AppConfig
from .db import ArchiveDatabase, utc_now
from .models import JobState, ensure_transition
from .resolver import Candidate, CandidateScore, ResolutionDecision, decide_resolution
from .tooling import CommandRunner, resolve_tool
from .urls import VIDEO_ID_PATTERN, parse_youtube_url


@dataclass(frozen=True)
class ResolutionRun:
    job_id: int
    state: JobState
    decision: ResolutionDecision


def _search_query(artist: str, title: str, version: str | None) -> str:
    parts = [artist.strip(), title.strip()]
    if version and version.strip():
        parts.append(version.strip())
    return " - ".join(parts[:2]) + (f" {parts[2]}" if len(parts) == 3 else "")


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _thumbnail(payload: dict[str, object]) -> str | None:
    direct = payload.get("thumbnail")
    if isinstance(direct, str) and direct:
        return direct
    thumbnails = payload.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url:
                    return url
    return None


def _candidate_from_payload(payload: dict[str, object]) -> Candidate | None:
    video_id = str(payload.get("id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not VIDEO_ID_PATTERN.fullmatch(video_id) or not title:
        return None
    channel = str(payload.get("channel") or payload.get("uploader") or "").strip()
    return Candidate(
        video_id=video_id,
        title=title,
        channel=channel,
        duration_seconds=_float_or_none(payload.get("duration")),
        url=f"https://www.youtube.com/watch?v={video_id}",
        thumbnail_url=_thumbnail(payload),
    )


def search_youtube_candidates(
    config: AppConfig,
    runner: CommandRunner,
    *,
    artist: str,
    title: str,
    version: str | None,
) -> tuple[Candidate, ...]:
    limit = max(1, int(config.candidate_limit))
    yt_dlp = resolve_tool(config.yt_dlp, config.tools_directory)
    target = f"ytsearch{limit}:{_search_query(artist, title, version)}"
    result = runner.run(
        (
            yt_dlp,
            "--ignore-config",
            "--flat-playlist",
            "--skip-download",
            "--dump-json",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
            "--extractor-retries",
            "2",
            "--playlist-end",
            str(limit),
            target,
        )
    )

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        payloads: list[dict[str, object]]
        if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
            payloads = [item for item in payload["entries"] if isinstance(item, dict)]
        elif isinstance(payload, dict):
            payloads = [payload]
        else:
            continue
        for item in payloads:
            candidate = _candidate_from_payload(item)
            if candidate is None or candidate.video_id in seen:
                continue
            seen.add(candidate.video_id)
            candidates.append(candidate)
            if len(candidates) >= limit:
                return tuple(candidates)
    return tuple(candidates)


def _insert_candidates(
    connection,
    job_id: int,
    ranked: tuple[CandidateScore, ...],
) -> None:
    connection.execute("DELETE FROM candidates WHERE job_id = ?", (job_id,))
    for position, scored in enumerate(ranked, start=1):
        candidate = scored.candidate
        connection.execute(
            """
            INSERT INTO candidates (
                job_id, position, video_id, url, title, channel,
                duration_seconds, thumbnail_url, score, reasons_json,
                warnings_json, disqualified
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                position,
                candidate.video_id,
                candidate.url or f"https://www.youtube.com/watch?v={candidate.video_id}",
                candidate.title,
                candidate.channel or None,
                candidate.duration_seconds,
                candidate.thumbnail_url,
                scored.score,
                json.dumps(scored.reasons, ensure_ascii=False),
                json.dumps(scored.warnings, ensure_ascii=False),
                1 if scored.disqualified else 0,
            ),
        )


def _persist_decision(
    database: ArchiveDatabase,
    job_id: int,
    decision: ResolutionDecision,
) -> ResolutionRun:
    now = utc_now()
    if decision.method == "automatic":
        target = JobState.READY
    elif decision.method == "needs_review":
        target = JobState.NEEDS_REVIEW
    else:
        target = JobState.NOT_FOUND

    selected = decision.selected.candidate if decision.selected else None
    with database.connect() as connection:
        row = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Job {job_id} does not exist")
        old_state = JobState(row["state"])
        if old_state != JobState.RESOLVING:
            raise ValueError(f"Job {job_id} must be resolving to record resolution")
        ensure_transition(old_state, target)
        _insert_candidates(connection, job_id, decision.ranked)
        completed_at = now if target == JobState.NOT_FOUND else None
        connection.execute(
            """
            UPDATE jobs
            SET state = ?, source_extractor = ?, source_id = ?, source_url = ?,
                source_title = ?, source_creator = ?, resolution_method = ?,
                selected_score = ?, runner_up_score = ?, updated_at_utc = ?,
                progress_percent = CASE WHEN ? = 'not_found' THEN 100 ELSE progress_percent END,
                completed_at_utc = CASE WHEN ? = 'not_found' THEN ? ELSE completed_at_utc END
            WHERE id = ?
            """,
            (
                target.value,
                "youtube" if selected else None,
                selected.video_id if selected else None,
                selected.url if selected else None,
                selected.title if selected else None,
                selected.channel if selected and selected.channel else None,
                decision.method,
                decision.selected_score,
                decision.runner_up_score,
                now,
                target.value,
                target.value,
                completed_at,
                job_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO job_events (
                job_id, occurred_at_utc, from_state, to_state,
                event_type, message, detail_json
            ) VALUES (?, ?, 'resolving', ?, 'resolution', ?, ?)
            """,
            (
                job_id,
                now,
                target.value,
                f"Resolution finished as {decision.method}",
                json.dumps(
                    {
                        "candidate_count": len(decision.ranked),
                        "selected_video_id": selected.video_id if selected else None,
                        "selected_score": decision.selected_score,
                        "runner_up_score": decision.runner_up_score,
                        "margin": decision.margin,
                    },
                    sort_keys=True,
                ),
            ),
        )
    return ResolutionRun(job_id=job_id, state=target, decision=decision)


def resolve_pending_job(
    database: ArchiveDatabase,
    config: AppConfig,
    runner: CommandRunner,
    job_id: int,
) -> ResolutionRun:
    job = database.get_job(job_id)
    if JobState(job["state"]) != JobState.PENDING:
        raise ValueError(f"Job {job_id} must be pending to resolve")
    artist = str(job["requested_artist"] or "").strip()
    title = str(job["requested_title"] or "").strip()
    version = str(job["requested_version"] or "").strip() or None
    if not artist or not title:
        raise ValueError(f"Job {job_id} has no artist/title resolution request")

    database.transition_job(
        job_id,
        JobState.RESOLVING,
        message=f"Searching up to {config.candidate_limit} YouTube candidates",
    )
    try:
        candidates = search_youtube_candidates(
            config,
            runner,
            artist=artist,
            title=title,
            version=version,
        )
        decision = decide_resolution(
            artist=artist,
            title=title,
            version=version,
            candidates=list(candidates),
            minimum_score=config.auto_select_min_score,
            minimum_margin=config.auto_select_min_margin,
        )
        return _persist_decision(database, job_id, decision)
    except Exception as exc:
        current = JobState(database.get_job(job_id)["state"])
        if current == JobState.RESOLVING:
            database.fail_job(job_id, stage="resolving", summary=str(exc))
        raise


def list_resolution_candidates(database: ArchiveDatabase, job_id: int):
    with database.connect() as connection:
        return list(
            connection.execute(
                "SELECT * FROM candidates WHERE job_id = ? ORDER BY position",
                (job_id,),
            )
        )


def approve_candidate(database: ArchiveDatabase, job_id: int, video_id: str) -> None:
    now = utc_now()
    with database.connect() as connection:
        job = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise KeyError(f"Job {job_id} does not exist")
        old_state = JobState(job["state"])
        if old_state != JobState.NEEDS_REVIEW:
            raise ValueError(f"Job {job_id} is not waiting for review")
        candidate = connection.execute(
            "SELECT * FROM candidates WHERE job_id = ? AND video_id = ?",
            (job_id, video_id),
        ).fetchone()
        if candidate is None:
            raise ValueError(f"Candidate {video_id} is not recorded for job {job_id}")
        ensure_transition(old_state, JobState.READY)
        connection.execute(
            """
            UPDATE jobs
            SET state = 'ready', source_extractor = 'youtube', source_id = ?, source_url = ?,
                source_title = ?, source_creator = ?, resolution_method = 'manual_selection',
                selected_score = ?, updated_at_utc = ?, completed_at_utc = NULL
            WHERE id = ?
            """,
            (
                candidate["video_id"],
                candidate["url"],
                candidate["title"],
                candidate["channel"],
                candidate["score"],
                now,
                job_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO job_events (
                job_id, occurred_at_utc, from_state, to_state,
                event_type, message, detail_json
            ) VALUES (?, ?, 'needs_review', 'ready', 'manual_resolution', ?, ?)
            """,
            (
                job_id,
                now,
                f"User approved candidate {video_id}",
                json.dumps({"video_id": video_id, "score": candidate["score"]}, sort_keys=True),
            ),
        )


def replace_source_url(database: ArchiveDatabase, job_id: int, url: str) -> None:
    pinned = parse_youtube_url(url)
    now = utc_now()
    with database.connect() as connection:
        job = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise KeyError(f"Job {job_id} does not exist")
        old_state = JobState(job["state"])
        if old_state != JobState.NEEDS_REVIEW:
            raise ValueError(f"Job {job_id} is not waiting for review")
        ensure_transition(old_state, JobState.READY)
        connection.execute(
            """
            UPDATE jobs
            SET state = 'ready', source_extractor = 'youtube', source_id = ?, source_url = ?,
                source_title = NULL, source_creator = NULL, resolution_method = 'manual_url',
                selected_score = NULL, runner_up_score = NULL,
                updated_at_utc = ?, completed_at_utc = NULL
            WHERE id = ?
            """,
            (pinned.video_id, pinned.canonical_url, now, job_id),
        )
        connection.execute(
            """
            INSERT INTO job_events (
                job_id, occurred_at_utc, from_state, to_state,
                event_type, message, detail_json
            ) VALUES (?, ?, 'needs_review', 'ready', 'manual_resolution', ?, ?)
            """,
            (
                job_id,
                now,
                f"User supplied replacement source {pinned.video_id}",
                json.dumps({"video_id": pinned.video_id, "url": pinned.canonical_url}, sort_keys=True),
            ),
        )


def mark_not_found(database: ArchiveDatabase, job_id: int) -> None:
    now = utc_now()
    with database.connect() as connection:
        job = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise KeyError(f"Job {job_id} does not exist")
        old_state = JobState(job["state"])
        if old_state != JobState.NEEDS_REVIEW:
            raise ValueError(f"Job {job_id} is not waiting for review")
        ensure_transition(old_state, JobState.NOT_FOUND)
        connection.execute(
            """
            UPDATE jobs
            SET state = 'not_found', source_extractor = NULL, source_id = NULL,
                source_url = NULL, source_title = NULL, source_creator = NULL,
                resolution_method = 'manual_not_found', progress_percent = 100,
                updated_at_utc = ?, completed_at_utc = ?
            WHERE id = ?
            """,
            (now, now, job_id),
        )
        connection.execute(
            """
            INSERT INTO job_events (
                job_id, occurred_at_utc, from_state, to_state,
                event_type, message
            ) VALUES (?, ?, 'needs_review', 'not_found', 'manual_resolution', ?)
            """,
            (job_id, now, "User marked the request not found"),
        )


def claim_next_queue_job(database: ArchiveDatabase, claim_token: str) -> int | None:
    if not claim_token:
        raise ValueError("Worker claim token is required")
    now = utc_now()
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM worker_claims LIMIT 1").fetchone():
            return None
        job = connection.execute(
            """
            SELECT id, state
            FROM jobs
            WHERE state IN ('pending', 'ready', 'converting')
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
