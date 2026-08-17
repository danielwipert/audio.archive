# Audio Archive — Session Handoff

**Last updated:** 2026-08-17  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `agent/windows-toolchain-setup`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Completed

- Native source-master acquisition was reviewed and squash-merged into `main`
  as commit `4a4409454a9b3bcb0984d263c4272ff65caf5911` (PR #1).
- Added project-first tool resolution, including the active virtual
  environment's Scripts directory for the pinned yt-dlp executable.
- Added non-mutating `audio-archive doctor` text and JSON diagnostics for:
  Python 3.11+, exact yt-dlp pin, Deno 2.3+, FFmpeg, FFprobe, yt-dlp EJS,
  loopback-only binding, and writable archive location.
- Reworked `scripts/setup.ps1` to create the virtual environment, install the
  pinned Python dependencies, locate or install Deno and FFmpeg through winget,
  bundle the external executables under `tools/`, initialize the archive, and
  require a passing readiness check.
- Added `scripts/update-tools.ps1` as the only explicit external-tool update
  path; ordinary ingestion never updates dependencies.
- Documented the setup workflow and portable-toolchain decision.

## Verification

- 37 deterministic tests pass.
- Changed Python files pass Ruff checks and all Python sources compile.
- A real local doctor run found the exact yt-dlp pin, EJS 0.8.0, FFmpeg, and
  FFprobe, and correctly returned `NOT READY` because this Linux build host has
  no Deno.
- Windows PowerShell execution is not available in this build host, so the
  setup script still requires its first Windows acceptance run.
- Live YouTube acquisition remains untested; it requires an authorized test URL.

## Project-use decision

Do not begin the permanent archive until all v0.3 acceptance criteria pass on
Windows. Pre-release media remains test material only.

## Exact next step

1. Review and merge the Windows toolchain PR.
2. Implement Ableton 32-bit float WAV conversion from the verified local source
   master, including real safe-size long-form segmentation and regeneration.
3. Add the archive verification command and PowerShell wrapper.
4. Run Windows setup acceptance, then one authorized live YouTube acquisition
   and audit its media, metadata, warnings, checksums, and archive structure.

Do not build the GUI until acquisition and derivative generation are reliable.
