# Audio Archive — Session Handoff

**Last updated:** 2026-08-19  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `agent/windows-acceptance`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Completed

- PR #5 added the recoverable sequential queue worker and is merged into `main`.
- PR #6 added YouTube candidate resolution/manual review and is merged into `main`.
- PR #10 built the single-screen local browser application and was squash-merged
  into `main` as commit `84272e5fe484ca4aefd962694248707f697ab063`.
- The normal user path now exists end to end in code: launch, add audio or CSV,
  resolve/review, queue, native acquisition, Ableton/listening derivatives,
  integrity verification, and completed-item handoff.
- Added `scripts/windows-acceptance.ps1` to run toolchain readiness, the full test
  suite, existing archive verification, optional authorized live complete-profile
  ingestion, and generation of a durable Markdown acceptance report.
- Added persistent local Ableton acceptance fixtures: one normal 32-bit float WAV
  and one forced-segmentation set produced through the same production Ableton
  conversion service without polluting permanent archive items.
- Added real-FFmpeg integration coverage for the acceptance fixture generator and
  documented the complete Windows v0.3 acceptance procedure.

## Verification

- PR #10 passed GitHub Actions CI before merge, including Ruff, compilation, and
  the full pytest suite.
- The Windows acceptance tooling has CI coverage for its Python fixture generator;
  the PowerShell runner itself must still be executed on the target Windows host.

## Project-use decision

Do not begin the permanent archive until the generated Windows acceptance report
has automated core PASS, one authorized live complete job has passed, and the
applicable manual launcher/Ableton checkboxes have been verified.

## Exact next step

1. Run CI on the Windows acceptance PR and merge it when clean.
2. On the target Windows machine, run `scripts\windows-acceptance.ps1 -RunSetup`.
3. Open the generated normal and segmented fixture WAVs in Ableton Live 12 and
   complete the manual launcher/browser/Ableton checkboxes in the report.
4. Run the acceptance script with one authorized YouTube URL, audit the resulting
   complete archive item, and remove the pre-release block only if all gates pass.
