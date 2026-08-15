# Audio Archive — Session Handoff

**Last updated:** 2026-08-14  
**Repository:** `danielwipert/audio.archive`  
**Branch:** `main`

## How to use this file

Read this file at the beginning of every working session. At the end of the
session, overwrite it with the current state of the project. Keep it short and
operational: what was completed, what was verified, what remains unresolved,
and the exact next step. This is a handoff checkpoint, not a cumulative history
or changelog.

Only describe work that is actually present on the repository branch named
above. Do not report uncommitted or unverified work as complete.

## Current state

The approved v0.3 specification has entered implementation. The first
application foundation is committed to `main` in foundation commit
`28df8ca386d2dea74fe24e7deb01b72803bb417c`.

Completed and verified:

- Portable TOML configuration and four output profiles
- SQLite schema version 1 for jobs, events, candidates, imports, archive items,
  and assets
- Audited job-state transitions and startup interruption recovery
- Manual, exact-URL, and CSV job creation through the shared Python core
- YouTube URL validation, canonicalization, and source-ID pinning
- CSV validation, row-level rejection, provenance, and within-import deduplication
- Deterministic resolver scoring and automatic-selection thresholds
- Ableton 32-bit float WAV size estimation and long-form segmentation planning
- Archive manifest JSON schema version 1.2
- Initial Windows setup and launcher scripts
- Twenty-two passing deterministic tests and a successful CLI smoke test

## Not yet implemented

- Live yt-dlp source search
- Native source-master download and controlled format selection
- FFprobe verification, checksums, warning classification, and atomic publication
- Ableton WAV conversion and real segmentation
- Listening MP3 derivative
- Background queue worker
- FastAPI browser interface and manual candidate-review screen
- Windows end-to-end acceptance testing

## Next step

Build the native-master acquisition vertical slice:

1. Add a controlled subprocess runner that ignores global yt-dlp configuration.
2. Acquire the pinned source with `bestaudio/best` into the job temporary directory.
3. Preserve yt-dlp info JSON, thumbnail, command outcome, warnings, and tool versions.
4. Verify the resulting master with FFprobe and SHA-256.
5. Classify acquisition quality without overstating “best available.”
6. Atomically publish the verified master and sidecars to the source-ID archive path.
7. Cover the entire slice with mocked deterministic tests before running a separate
   authorized network integration test.

Do not build the GUI before this pipeline slice is reliable; the CLI and future
browser interface must call the same acquisition core.

## Locked implementation decisions

- Ableton Live 12 is the primary target, with the 1.8 GiB segmentation threshold
  retained for earlier-version compatibility.
- The default archive root is the portable project-relative `archive/` directory.
- yt-dlp is pinned to `2026.7.4`; dependency upgrades are explicit maintenance.
- SQLite owns application state; `archive.json`, source info JSON, and checksums
  preserve each archive item independently.
