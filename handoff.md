# Audio Archive — Session Handoff

**Last updated:** 2026-08-17  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `agent/native-master-acquisition`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.
Describe only work present on the branch named above.

## Completed on this branch

The native source-master acquisition vertical slice is implemented:

- Shell-free controlled subprocess execution and portable tool resolution
- Required Deno 2.3+ runtime with explicit yt-dlp runtime selection
- yt-dlp default EJS dependency group pinned with the application version
- `--ignore-config`, `--no-playlist`, and `bestaudio/best` acquisition policy
- Pinned video-ID verification before publication
- Unmodified source info JSON and source-thumbnail preservation
- FFprobe inspection requiring exactly one audio stream in the source master
- Combined-stream fallback demuxed with FFmpeg codec copy and no re-encoding
- Quality-warning classification for JavaScript, challenge, token,
  authentication, format, region, and throttling limitations
- Quality status protection against overstating “verified best available”
- SHA-256 generation and archive-item integrity verification
- Atomic publication under the stable YouTube video ID
- Existing-item integrity validation and acquisition reuse
- SQLite acquisition records, asset records, stage transitions, and failure state
- Retained diagnostic logs when acquisition fails
- Development CLI entry point for one ready job

Validation completed:

- 34 deterministic tests pass.
- A locally generated WAV passed the real FFprobe execution path.
- Python source compiles and the archive manifest schema remains valid JSON.

Not run:

- Live YouTube acquisition. The current build environment lacks yt-dlp and Deno,
  and no authorized network-test URL was supplied. Do not imply this passed.

## Project-use decision

Do not begin the permanent archive with a partial build. Normal use begins only
after all v0.3 first-release acceptance criteria pass on Windows. Pre-release
media is test material only.

## Next step

Review and merge the native-master acquisition branch. Then:

1. Finish portable Windows installation and dependency diagnostics for yt-dlp,
   matching EJS components, Deno, FFmpeg, and FFprobe.
2. Run one authorized live acquisition integration test and audit its master,
   metadata, warning status, checksums, and archive structure.
3. Build Ableton 32-bit float WAV conversion, real long-form segmentation, and
   derivative regeneration from the verified local source master.

Do not build the GUI until the shared acquisition and derivative pipeline is
reliable.
