# Audio Archive - Session Handoff

**Last updated:** 2026-09-03
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `main`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Current verified state

- `main` is at 3953111. PRs #25, #27, #28, #29 and #30 landed a spec-versus-repo review
  and its six findings. DEC-012 to DEC-015 record the decisions; the PR bodies hold the
  detail.
- Cloud v0.1 now classifies YouTube access failures, requeues the transient ones itself
  at 5, 10 and 20 minutes, reuses a failed attempt's verified scratch instead of
  downloading again, offers Ableton WAV / 24-bit WAV / MP3 / package per job, accepts CSV
  imports, and has pause, cancel and early deletion.
- Proxy credentials no longer reach the published `ingest.log`. The Deno minimum is 2.4.3,
  matching the pinned BgUtils provider, and `doctor` reads the yt-dlp pin from the
  installed distribution - which also repaired a stale constant that was blocking
  `launch.cmd`.
- Verification: 203 tests pass with PostgreSQL and FFmpeg available, nothing skipped. That
  included the DEC-008 byte-identical segmentation test, which had been skipped in every
  previous session.
- Schema is at migration 4. Deploy the web service before the worker; the worker waits for
  the migrations it ships with and logs which ones it is waiting on.
- New worker variables, all defaulted: `AUDIO_ARCHIVE_ACCESS_RETRY_LIMIT` (3),
  `AUDIO_ARCHIVE_ACCESS_RETRY_BASE_SECONDS` (300),
  `AUDIO_ARCHIVE_SCRATCH_RETENTION_HOURS` (6). Web adds `AUDIO_ARCHIVE_MAX_CSV_BYTES`.
- Retained scratch is a real change in worker disk footprint: a failed job holds its
  source master until the sweep reclaims it. Lower the retention if disk is tight.
- **Unchanged and still unanswered:** whether Railway egress can reach YouTube. The
  429 that stopped the last production job has not been diagnosed. Everything above is
  instrumentation and resilience around that question, not an answer to it.

## Cloud v0.1 boundary

Cloud media remains temporary. PostgreSQL retains job/history metadata after media expiry,
while verified source/Ableton/package files are delivered from private R2 and removed after
the retention window. Source-quality rules remain unchanged.

## Exact next step

1. Confirm Railway redeployed web and then worker from `main`.
2. Check the worker startup log for `YouTube proxy routing is enabled`. If the line is
   absent, `AUDIO_ARCHIVE_YTDLP_PROXY` is not set on the worker and that alone explains
   the 429.
3. If proxy routing is on, check the DataImpulse balance before changing any code.
4. Retry the Babehoven exact URL with the Ableton output. The job page now names the
   failure class, so read it rather than the raw tool output.
5. If three automatic retries fail across about 35 minutes with proxy routing confirmed
   healthy, that is the evidence for the decision `CLOUD_SPEC.md` section 9.2 and open
   decision 9 anticipate: move acquisition to a residential worker. The job model and
   `worker_network_class` already support it.

## Known gaps

- Windows/Ableton acceptance and one authorized live end-to-end acquisition still gate
  the permanent archive under DEC-005.
- Long-form segmentation is proven by local tests but has never run a cloud job.
- An abandoned CSV preview leaves a staged file until the web container recycles.
