# Audio Archive — Session Handoff

**Last updated:** 2026-08-17  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `agent/sequential-worker`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Completed

- PR #4 (verified listening MP3 derivatives) was reviewed and squash-merged
  into `main` as commit `5d74d2d06da7f8242fab053617287d58ca5d0bd9`.
- Added the single sequential worker required by v0.3. It claims one runnable
  SQLite job and runs acquisition plus all requested derivatives automatically.
- Added exclusive process-aware claims so a second launcher cannot interrupt a
  live worker; stale crash claims are cleared during safe startup recovery.
- Added interrupted-job recovery from durable boundaries, output reuse, queue
  continuation after recorded failures, pause-after-current/resume primitives,
  retry counters, cancellation, and schema 1-to-2 migration.
- Added `run-queue`, `run-queue --once`, `retry JOB_ID`, and `cancel JOB_ID`.

## Verification

- 59 tests pass; every changed Python file passes Ruff and compilation checks.
- Tests cover exclusive claims, live-worker protection, stale-claim recovery,
  interrupted requeue, retry history, pause/resume, failure isolation, pending
  review bypass, sequential ordering, complete-profile stages, and DB migration.
- Windows PowerShell/Ableton acceptance and an authorized live YouTube run are
  still required before permanent archive use.

## Project-use decision

Do not begin the permanent archive until all v0.3 acceptance criteria pass on
Windows. Pre-release media remains test material only.

## Exact next step

1. Review and merge the sequential worker PR.
2. Implement yt-dlp candidate search, deterministic resolution persistence, and
   manual candidate approval/replacement/not-found actions.
3. Build the single-screen local FastAPI browser interface on the shared queue.
4. Run Windows setup and Ableton acceptance, then one authorized live YouTube
   `complete` job and audit the finished archive item.
