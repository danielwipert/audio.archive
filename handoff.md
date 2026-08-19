# Audio Archive — Session Handoff

**Last updated:** 2026-08-19  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `agent/source-resolver`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Completed

- PR #5 (recoverable sequential queue worker) was reviewed, corrected for safe
  Windows process liveness, and merged into `main` as commit
  `171f44d60555f11804f8c4faff223df96e05166b`.
- Added bounded metadata-only yt-dlp candidate search for pending artist/title
  jobs and connected it to the existing deterministic resolver scoring policy.
- Persisted ranked candidates, scores, reasons, warnings, and disqualification
  evidence before automatic selection or review.
- Added automatic source pinning plus manual candidate approval, replacement URL,
  and not-found actions; review jobs do not block later queue work.
- Connected pending resolution to the sequential worker so strong matches can
  continue directly into acquisition under the same worker claim.
- Added resolver CLI controls and repository CI for Ruff, compilation, and the
  full pytest suite.

## Verification

- Resolver fixtures cover automatic selection, ambiguous review, manual approval,
  replacement URL, not-found, queue claiming, and worker continuation behavior.
- GitHub Actions CI is the merge gate for this branch and must pass before merge.
- Windows PowerShell/Ableton acceptance and an authorized live YouTube run are
  still required before permanent archive use.

## Project-use decision

Do not begin the permanent archive until all v0.3 acceptance criteria pass on
Windows. Pre-release media remains test material only.

## Exact next step

1. Review CI and merge the source-resolver PR if clean.
2. Build the single-screen local FastAPI browser interface on the shared queue,
   including add-audio, CSV preview/import, queue status, and candidate review.
3. Add the Windows launcher behavior that starts the loopback server and opens
   the default browser while preserving worker recovery semantics.
4. Run Windows setup and Ableton acceptance, then one authorized live YouTube
   `complete` job and audit the finished archive item.
