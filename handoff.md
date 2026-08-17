# Audio Archive — Session Handoff

**Last updated:** 2026-08-17  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `agent/ableton-derivatives`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Completed

- PR #2 (portable Windows toolchain and `doctor`) was reviewed and squash-merged
  into `main` as commit `6ed213965a67ceec44d701e8ef5de7f5a5b8e882`.
- Added local-source-only Ableton conversion to 32-bit float PCM WAV.
- Conversion preserves source sample rate and mono/stereo channels and supplies
  no normalization, resampling, channel remixing, filters, or dither.
- Added safe-size planning that shortens segments for unusually high source
  rates so every generated WAV remains beneath the configured threshold.
- Added one-pass long-form segmentation with exact sample counts, contiguous
  start/end sample records, ordered filenames, FFprobe validation, and SHA-256.
- Added transactional publication with manifest/checksum rollback on failure.
- Added offline reuse of an existing valid Ableton output and creation of a
  previously missing output without another YouTube request.
- Added SQLite Ableton asset records and job-state integration. `ableton`
  profiles complete only after verification; `complete` remains converting
  until its listening output also exists.
- Added `audio-archive convert-ableton JOB_ID`.
- Added `audio-archive verify VIDEO_ID`, `verify --all`, and
  `scripts/verify-archive.ps1` to cross-check SHA256SUMS, manifest inventory, and
  every manifest-recorded asset.

## Verification

- 46 tests pass and all changed Python files pass Ruff and compilation checks.
- A real FFmpeg test generated source audio, forced segmentation, and proved
  that concatenated segment PCM is byte-identical to one unsegmented 32-bit
  float decode.
- Manifest assets, SQLite assets, contiguous sample boundaries, output reuse,
  high-rate size limits, tamper detection, and profile completion are covered.
- Windows PowerShell and Ableton execution are unavailable on this build host;
  Windows setup and Ableton-open acceptance remain outstanding.
- Live YouTube acquisition remains untested and requires an authorized test URL.

## Project-use decision

Do not begin the permanent archive until all v0.3 acceptance criteria pass on
Windows. Pre-release media remains test material only.

## Exact next step

1. Review and merge the Ableton derivative PR.
2. Implement the optional listening MP3 directly from the verified local source
   master, including metadata, artwork, reuse, manifests, checksums, and the
   `listen`/`complete` profile completion rules.
3. Build the background worker around the shared acquisition/derivative pipeline.
4. Run Windows setup and Ableton acceptance, then one authorized live YouTube
   acquisition and audit the complete archive item.

Do not build the GUI until all shared pipeline stages are reliable.
