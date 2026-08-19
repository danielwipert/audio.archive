# Audio Archive — Session Handoff

**Last updated:** 2026-08-19  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `main`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Completed

- PR #5 added the recoverable sequential queue worker and is merged into `main`.
- PR #6 added YouTube candidate resolution/manual review and is merged into `main`.
- PR #10 built the single-screen local browser application and was squash-merged
  into `main` as commit `84272e5fe484ca4aefd962694248707f697ab063`.
- PR #11 added Windows v0.3 release acceptance tooling and was squash-merged into
  `main` as commit `a1971d6e3cff889ea8e4eaeff96f121615896020`.
- The normal user path now exists end to end in code: launch, add audio or CSV,
  resolve/review, queue, native acquisition, Ableton/listening derivatives,
  integrity verification, and completed-item handoff.
- `scripts/windows-acceptance.ps1` now runs toolchain readiness, the full test
  suite, existing archive verification, optional authorized live complete-profile
  ingestion, and writes a durable Markdown acceptance report.
- Persistent local Ableton acceptance fixtures provide one normal 32-bit float WAV
  and one forced-segmentation set through the same production conversion service.

## Verification

- PR #10 passed GitHub Actions CI before merge: Ruff, compilation, and pytest.
- PR #11 passed GitHub Actions CI before merge: Ruff, compilation, PowerShell
  syntax validation, pytest, and real-FFmpeg acceptance-fixture coverage.
- Repository-side v0.3 build work is complete; remaining gates require the target
  Windows/Ableton machine and an authorized live YouTube source.

## Project-use decision

Do not begin the permanent archive until the generated Windows acceptance report
has automated core PASS, one authorized live complete job has passed, and the
applicable manual launcher/Ableton checkboxes have been verified.

## Exact next step

1. On the target Windows machine, run `scripts\windows-acceptance.ps1 -RunSetup`.
2. Open the generated normal and segmented fixture WAVs in Ableton Live 12 and
   complete the manual launcher/browser/Ableton checkboxes in the report.
3. Run the acceptance script with one authorized YouTube URL, audit the resulting
   complete archive item, and remove the pre-release block only if all gates pass.
