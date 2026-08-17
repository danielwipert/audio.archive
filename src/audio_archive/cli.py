from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .db import ArchiveDatabase
from .inputs import attach_import_id, normalize_request, preview_csv
from .models import JobState
from .acquisition import ExistingArchiveConflict
from .pipeline import acquire_ready_job
from .tooling import SubprocessRunner, ToolExecutionError


def _database() -> tuple[ArchiveDatabase, object]:
    config = load_config()
    database = ArchiveDatabase(config.database_path)
    database.initialize()
    return database, config


def _add_command(args: argparse.Namespace) -> int:
    database, _ = _database()
    request = normalize_request(
        artist=args.artist,
        title=args.title,
        version=args.version,
        url=args.url,
        profile=args.profile,
        origin="cli",
    )
    job_id = database.create_job(request)
    print(f"Created job {job_id}")
    return 0


def _import_csv_command(args: argparse.Namespace) -> int:
    database, config = _database()
    preview = preview_csv(Path(args.path), max_bytes=config.max_csv_bytes)
    print(
        f"Accepted: {len(preview.accepted)}; rejected: {len(preview.rejected)}; "
        f"duplicates: {len(preview.duplicate_rows)}"
    )
    for rejection in preview.rejected:
        print(f"Rejected row {rejection.row_number}: {rejection.message}")
    if args.preview:
        return 0
    import_id = database.create_csv_import(
        filename=preview.filename,
        file_sha256=preview.file_sha256,
        accepted_rows=len(preview.accepted),
        rejected_rows=len(preview.rejected),
        duplicate_rows=len(preview.duplicate_rows),
    )
    job_ids = [database.create_job(attach_import_id(item, import_id)) for item in preview.accepted]
    print(f"Created jobs: {', '.join(map(str, job_ids)) if job_ids else 'none'}")
    return 0


def _list_command(args: argparse.Namespace) -> int:
    database, _ = _database()
    state = JobState(args.state) if args.state else None
    jobs = database.list_jobs(state)
    for job in jobs:
        request = " — ".join(
            part for part in (job["requested_artist"], job["requested_title"]) if part
        ) or job["requested_url"]
        print(f"{job['id']:>4}  {job['state']:<24} {job['profile']:<9} {request}")
    return 0


def _acquire_command(args: argparse.Namespace) -> int:
    database, config = _database()
    result = acquire_ready_job(database, config, SubprocessRunner(), args.job_id)
    print(f"Source master: {result.master_path}")
    print(f"Quality status: {result.quality_status}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audio-archive")
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="initialize archive directories and database")
    initialize.set_defaults(handler=lambda _: _init_command())

    add = subparsers.add_parser("add", help="create one persistent ingestion job")
    add.add_argument("--artist")
    add.add_argument("--title")
    add.add_argument("--version")
    add.add_argument("--url")
    add.add_argument("--profile", default="ableton")
    add.set_defaults(handler=_add_command)

    import_csv = subparsers.add_parser("import-csv", help="validate or import a CSV batch")
    import_csv.add_argument("path")
    import_csv.add_argument("--preview", action="store_true")
    import_csv.set_defaults(handler=_import_csv_command)

    list_jobs = subparsers.add_parser("list", help="list persistent jobs")
    list_jobs.add_argument("--state", choices=[state.value for state in JobState])
    list_jobs.set_defaults(handler=_list_command)

    acquire = subparsers.add_parser(
        "acquire", help="run the native-master stage for one ready job"
    )
    acquire.add_argument("job_id", type=int)
    acquire.set_defaults(handler=_acquire_command)
    return parser


def _init_command() -> int:
    database, config = _database()
    config.temp_directory.mkdir(parents=True, exist_ok=True)
    (config.archive_root / "items" / "youtube").mkdir(parents=True, exist_ok=True)
    interrupted = database.interrupt_active_jobs()
    print(f"Archive initialized at {config.archive_root}")
    if interrupted:
        print(f"Marked {interrupted} active job(s) interrupted for recovery")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        ExistingArchiveConflict,
        FileNotFoundError,
        KeyError,
        ToolExecutionError,
        ValueError,
    ) as exc:
        parser.error(str(exc))
    return 2
