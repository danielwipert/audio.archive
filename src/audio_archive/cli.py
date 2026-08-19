from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import threading
import webbrowser

from .ableton import ExistingDerivativeConflict
from .acquisition import ExistingArchiveConflict
from .archive_verify import verify_archive_item
from .config import load_config
from .db import ArchiveDatabase
from .doctor import format_report, run_doctor
from .inputs import attach_import_id, normalize_request, preview_csv
from .models import JobState
from .pipeline import acquire_ready_job, create_ableton_for_job, create_listening_for_job
from .source_resolution import (
    approve_candidate,
    list_resolution_candidates,
    mark_not_found,
    replace_source_url,
    resolve_pending_job,
)
from .tooling import SubprocessRunner, ToolExecutionError
from .worker import SequentialWorker


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


def _resolve_command(args: argparse.Namespace) -> int:
    database, config = _database()
    result = resolve_pending_job(database, config, SubprocessRunner(), args.job_id)
    print(
        f"Job {args.job_id}: {result.state.value} "
        f"({len(result.decision.ranked)} candidate(s), {result.decision.method})"
    )
    if result.decision.selected:
        selected = result.decision.selected
        print(
            f"Selected {selected.candidate.video_id}: {selected.candidate.title} "
            f"[{selected.score}]"
        )
    return 0


def _candidates_command(args: argparse.Namespace) -> int:
    database, _ = _database()
    rows = list_resolution_candidates(database, args.job_id)
    if not rows:
        print("No recorded candidates")
        return 0
    for row in rows:
        warnings = json.loads(row["warnings_json"])
        suffix = f" warnings={'; '.join(warnings)}" if warnings else ""
        print(
            f"{row['position']:>2}. {row['score']:>3}  {row['video_id']}  "
            f"{row['title']} — {row['channel'] or 'unknown channel'}{suffix}"
        )
    return 0


def _approve_command(args: argparse.Namespace) -> int:
    database, _ = _database()
    approve_candidate(database, args.job_id, args.video_id)
    print(f"Job {args.job_id}: approved {args.video_id}")
    return 0


def _replace_source_command(args: argparse.Namespace) -> int:
    database, _ = _database()
    replace_source_url(database, args.job_id, args.url)
    print(f"Job {args.job_id}: replacement source pinned")
    return 0


def _not_found_command(args: argparse.Namespace) -> int:
    database, _ = _database()
    mark_not_found(database, args.job_id)
    print(f"Job {args.job_id}: marked not found")
    return 0


def _acquire_command(args: argparse.Namespace) -> int:
    database, config = _database()
    result = acquire_ready_job(database, config, SubprocessRunner(), args.job_id)
    print(f"Source master: {result.master_path}")
    print(f"Quality status: {result.quality_status}")
    return 0


def _doctor_command(args: argparse.Namespace) -> int:
    config = load_config()
    report = run_doctor(config, SubprocessRunner())
    print(json.dumps(report.as_dict(), indent=2) if args.json else format_report(report))
    return 0 if report.ready else 1


def _convert_ableton_command(args: argparse.Namespace) -> int:
    database, config = _database()
    result = create_ableton_for_job(database, config, SubprocessRunner(), args.job_id)
    if result.segmented:
        print(f"Ableton segments: {result.assets[0].path.parent}")
    else:
        print(f"Ableton WAV: {result.assets[0].path}")
    print(f"Reused existing output: {'yes' if result.reused_existing else 'no'}")
    return 0


def _convert_listening_command(args: argparse.Namespace) -> int:
    database, config = _database()
    result = create_listening_for_job(database, config, SubprocessRunner(), args.job_id)
    print(f"Listening MP3: {result.asset.path}")
    print(f"Reused existing output: {'yes' if result.reused_existing else 'no'}")
    return 0


def _run_queue_command(args: argparse.Namespace) -> int:
    database, config = _database()
    worker = SequentialWorker(database, config, SubprocessRunner())
    recovery = worker.recover_startup()
    if recovery.interrupted_jobs or recovery.stale_claims:
        print(
            f"Recovery: {recovery.interrupted_jobs} interrupted, "
            f"{recovery.stale_claims} stale claim(s), {recovery.requeued_jobs} requeued"
        )
    results = (worker.run_next(),) if args.once else worker.run_until_idle()
    completed = [result for result in results if result is not None]
    if not completed:
        print("No runnable jobs")
        return 0
    failed = False
    for result in completed:
        print(f"Job {result.job_id}: {result.state.value}")
        if result.error:
            print(f"  {result.error}")
        failed = failed or result.state == JobState.FAILED
    return 1 if failed else 0


def _retry_command(args: argparse.Namespace) -> int:
    database, _ = _database()
    state = database.retry_job(args.job_id)
    print(f"Job {args.job_id} queued for retry as {state.value}")
    return 0


def _cancel_command(args: argparse.Namespace) -> int:
    database, _ = _database()
    database.transition_job(args.job_id, JobState.CANCELLED, message="Cancelled by user")
    print(f"Cancelled job {args.job_id}")
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    config = load_config()
    if args.all:
        items_root = config.archive_root / "items" / "youtube"
        item_directories = (
            sorted(path for path in items_root.iterdir() if path.is_dir())
            if items_root.is_dir()
            else []
        )
        if not item_directories:
            print("No archive items found")
            return 0
    else:
        target = Path(args.target)
        item_directories = [
            target.resolve()
            if target.is_dir()
            else config.archive_root / "items" / "youtube" / args.target
        ]
    valid = True
    for item_directory in item_directories:
        result = verify_archive_item(item_directory)
        status = "OK" if result.valid else "FAIL"
        identity = result.archive_id or item_directory.name
        print(f"[{status}] {identity}: {result.checked_files} checked file(s)")
        for error in result.errors:
            print(f"  - {error}")
        valid = valid and result.valid
    return 0 if valid else 1


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _serve_command(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    config = load_config()
    if not _is_loopback_host(config.host):
        raise ValueError(f"Refusing to expose Audio Archive on non-loopback host {config.host!r}")
    url = f"http://{config.host}:{config.port}/"
    if config.open_browser and not args.no_browser:
        opener = threading.Timer(0.7, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    print(f"Audio Archive: {url}")
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audio-archive")
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="initialize archive directories and database")
    initialize.set_defaults(handler=lambda _: _init_command())

    serve = subparsers.add_parser("serve", help="start the loopback-only local browser application")
    serve.add_argument("--no-browser", action="store_true", help="do not open the default browser")
    serve.set_defaults(handler=_serve_command)

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

    resolve = subparsers.add_parser("resolve", help="search and score one pending artist/title job")
    resolve.add_argument("job_id", type=int)
    resolve.set_defaults(handler=_resolve_command)

    candidates = subparsers.add_parser("candidates", help="show recorded candidates for one job")
    candidates.add_argument("job_id", type=int)
    candidates.set_defaults(handler=_candidates_command)

    approve = subparsers.add_parser("approve", help="approve one recorded candidate")
    approve.add_argument("job_id", type=int)
    approve.add_argument("video_id")
    approve.set_defaults(handler=_approve_command)

    replace_source = subparsers.add_parser(
        "replace-source", help="pin a replacement YouTube URL for a review job"
    )
    replace_source.add_argument("job_id", type=int)
    replace_source.add_argument("url")
    replace_source.set_defaults(handler=_replace_source_command)

    not_found = subparsers.add_parser("not-found", help="mark a review job not found")
    not_found.add_argument("job_id", type=int)
    not_found.set_defaults(handler=_not_found_command)

    acquire = subparsers.add_parser(
        "acquire", help="run the native-master stage for one ready job"
    )
    acquire.add_argument("job_id", type=int)
    acquire.set_defaults(handler=_acquire_command)

    convert_ableton = subparsers.add_parser(
        "convert-ableton", help="create or reuse a verified Ableton intermediate"
    )
    convert_ableton.add_argument("job_id", type=int)
    convert_ableton.set_defaults(handler=_convert_ableton_command)

    convert_listening = subparsers.add_parser(
        "convert-listening", help="create or reuse a verified listening MP3"
    )
    convert_listening.add_argument("job_id", type=int)
    convert_listening.set_defaults(handler=_convert_listening_command)

    run_queue = subparsers.add_parser(
        "run-queue", help="recover and process queued jobs sequentially"
    )
    run_queue.add_argument("--once", action="store_true", help="process at most one job")
    run_queue.set_defaults(handler=_run_queue_command)

    retry = subparsers.add_parser("retry", help="requeue one failed or interrupted job")
    retry.add_argument("job_id", type=int)
    retry.set_defaults(handler=_retry_command)

    cancel = subparsers.add_parser("cancel", help="cancel one waiting job")
    cancel.add_argument("job_id", type=int)
    cancel.set_defaults(handler=_cancel_command)

    verify = subparsers.add_parser("verify", help="verify archive checksums and manifest assets")
    verify_target = verify.add_mutually_exclusive_group(required=True)
    verify_target.add_argument("target", nargs="?", help="video ID or archive item directory")
    verify_target.add_argument("--all", action="store_true", help="verify every YouTube item")
    verify.set_defaults(handler=_verify_command)

    doctor = subparsers.add_parser("doctor", help="verify the complete local toolchain")
    doctor.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")
    doctor.set_defaults(handler=_doctor_command)
    return parser


def _init_command() -> int:
    database, config = _database()
    config.temp_directory.mkdir(parents=True, exist_ok=True)
    (config.archive_root / "items" / "youtube").mkdir(parents=True, exist_ok=True)
    recovery = SequentialWorker(database, config, SubprocessRunner()).recover_startup()
    print(f"Archive initialized at {config.archive_root}")
    if recovery.interrupted_jobs or recovery.stale_claims:
        print(
            f"Recovered {recovery.requeued_jobs} interrupted job(s) and cleared "
            f"{recovery.stale_claims} stale worker claim(s)"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        ExistingArchiveConflict,
        ExistingDerivativeConflict,
        FileNotFoundError,
        KeyError,
        ToolExecutionError,
        ValueError,
    ) as exc:
        parser.error(str(exc))
    return 2
