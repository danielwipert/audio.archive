# Audio Archive - Session Handoff

**Last updated:** 2026-09-03
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `main`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Current verified state

- `main` is at cb8ac01. PRs #25, #27, #28, #29 and #30 landed a spec-versus-repo review
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
- **The 429 blocker is resolved.** Job 8 ran end to end from Railway on 2026-09-03:
  acquisition, `verified_best_available` with no fallback and no quality warnings, a
  verified 24-bit WAV, checksummed sidecars, and signed downloads with a 24-hour expiry.
  The proxy path works. Four sessions of BgUtils and client tuning are finished.
- PR #32 fixed the one thing that broke first: `wav24` was missing from the storage
  layer's delivery-role list, so a job failed at publishing after its audio had already
  been acquired, converted and verified. The role list is derived from one place now.
- The BgUtils HTTP token server (PR #34) is deployed and working: jobs after it show no
  PO token, BotGuard or script-timeout warnings at all, where job 9 showed all three.
- Job 11 ran the archive package end to end from Railway on 2026-09-03:
  `verified_best_available`, no quality warnings, a downloadable `audio-archive.zip`
  alongside the native source master, checksummed sidecars and thumbnail. Every cloud
  output - Ableton WAV, 24-bit WAV, MP3 and now the archive package - has produced a
  downloadable file in production.

## Cloud v0.1 boundary

Cloud media remains temporary. PostgreSQL retains job/history metadata after media expiry,
while verified source/Ableton/package files are delivered from private R2 and removed after
the retention window. Source-quality rules remain unchanged.

## Exact next step

Cloud v0.1 works end to end. What remains is proving the paths that have never run in
production, in rising order of cost:

1. Run one CSV import, which has never run in production. It is the last untried path
   short of the DEC-005 gate itself.
2. Run one long-form item past the 1.8 GiB threshold, to exercise segmentation in the
   cloud. It is proven by local FFmpeg tests and by nothing else.
3. Then the DEC-005 gate for the permanent archive: Windows and Ableton acceptance plus
   one authorized live end-to-end acquisition on the target machine.

## Known gaps

- Windows/Ableton acceptance and one authorized live end-to-end acquisition still gate
  the permanent archive under DEC-005.
- Long-form segmentation and CSV import are each proven by tests and unproven in
  production.
- The proxy intermittently returns truncated or non-TLS responses: jobs 9 and 10 show
  `[SSL: WRONG_VERSION_NUMBER]` and `Incomplete data received in embedded initial data`.
  yt-dlp retries and falls back to the API, so jobs succeed, but a page-derived format
  list may be incomplete and the status is then `best_available_with_warnings`. The
  exact-URL job 8, which fetches one page, came back clean. This is a proxy-side matter,
  not a code one; yt-dlp is already on the newest release (2026.8.19).
- An abandoned CSV preview leaves a staged file until the web container recycles.
- The stray branch `claude/fix-wav24-delivery-role` on GitHub duplicates a commit that
  is already in `main`; delete it from the branches page.
