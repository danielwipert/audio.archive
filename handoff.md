# Audio Archive — Session Handoff

**Last updated:** 2026-08-17  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `agent/listening-derivatives`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Completed

- PR #3 (verified Ableton derivative pipeline) was reviewed and squash-merged
  into `main` as commit `d331426d36e0f6083495b73896b4ac6ac8bde78c`.
- Added optional local-source-only listening MP3 generation with libmp3lame VBR
  quality scale 0; no YouTube request is made during derivative creation.
- Added curated title/artist and embedded source-thumbnail artwork, with FFprobe
  verification of MP3 audio, source rate/channels, tags, and attached picture.
- Added transactional manifest/checksum/log publication, source/output hashes,
  encoder settings, conflict protection, and offline reuse of valid output.
- Added SQLite listening assets, `audio-archive convert-listening JOB_ID`, and
  order-independent completion for `listen` and `complete` profiles.

## Verification

- 50 tests pass; every changed Python file passes Ruff and compilation checks.
- A real FFmpeg test generates audio and artwork, creates the MP3, and verifies
  the codec, rate, channels, curated tags, attached picture, manifest, and sums.
- Windows PowerShell/Ableton acceptance and an authorized live YouTube run are
  still required before permanent archive use.

## Project-use decision

Do not begin the permanent archive until all v0.3 acceptance criteria pass on
Windows. Pre-release media remains test material only.

## Exact next step

1. Review and merge the listening derivative PR.
2. Build the recoverable background worker that claims queued jobs and runs the
   shared acquisition and requested derivative stages.
3. Complete resolver search/manual candidate review, then the local browser UI.
4. Run Windows setup and Ableton acceptance, then one authorized live YouTube
   acquisition and audit a complete archive item.
