# Audio Archive — Session Handoff

**Last updated:** 2026-08-19  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `agent/browser-ui`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Completed

- PR #5 (recoverable sequential queue worker) is merged into `main`.
- PR #6 (YouTube candidate resolution and manual review) passed CI and was
  squash-merged into `main` as commit `17a84d4ad656eb9fae765069103615362a270a19`.
- PR #8 made CI runner-local and deterministic; Ruff baseline, compilation, and
  pytest now run without network package mutation.
- Built the single-screen FastAPI browser application using the shared SQLite
  queue, resolver, acquisition, derivative, retry, cancel, and review paths.
- Added artist/title and exact-URL submission, CSV preview/import, live queue
  polling, pause/resume controls, candidate review, replacement URL, not-found,
  completed-item folder access, and Ableton path copy.
- Added a daemon queue controller so yt-dlp/FFmpeg work stays off HTTP request
  handling while preserving sequential-worker claims and explicit queue pause.
- Added `audio-archive serve` with a hard loopback-only host guard and automatic
  browser opening; `launch.cmd` now runs readiness diagnostics then the app.
- Added HTTP and queue-control tests for manual input, exact URLs, CSV provenance,
  review actions, single-use preview tokens, pause/resume, and loopback safety.

## Verification

- Browser UI CI is the merge gate for this branch and must pass before merge.
- Source/imported metadata is inserted into the DOM as text, not executable HTML.
- Windows PowerShell/Ableton acceptance and an authorized live YouTube run are
  still required before permanent archive use.

## Project-use decision

Do not begin the permanent archive until all v0.3 acceptance criteria pass on
Windows. Pre-release media remains test material only.

## Exact next step

1. Run CI on the browser UI PR and correct any failures before merge.
2. Review and merge the browser UI slice into `main` when clean.
3. Run the full Windows setup/launcher acceptance and open normal and segmented
   generated WAVs in the supported Ableton target.
4. Run one authorized live YouTube `complete` job, verify the full archive item,
   and close any remaining v0.3 acceptance gaps before normal archive use.
