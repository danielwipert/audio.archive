# Audio Archive - Session Handoff

**Last updated:** 2026-09-02
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `main`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Current verified state

- Cloud v0.1 is deployed on Railway with separate web/worker services, PostgreSQL,
  private R2 temporary delivery, and Cloudflare Access.
- PR #24 is merged to `main`. It aligned both BgUtils halves at 1.3.2, updated yt-dlp to
  2026.8.19, removed the forced `mweb` YouTube client, added bounded network retries, and
  capped each worker subprocess at 20 minutes.
- The first production job after PR #24 (exact URL, `ableton` profile) failed at
  `downloading` 13 seconds in, with HTTP 429 on the webpage fetch plus
  `Sign in to confirm you're not a bot`. No media transferred. Whether
  `AUDIO_ARCHIVE_YTDLP_PROXY` survived the redeploy has not been confirmed.
- Removing the `mweb` pin means yt-dlp now fetches the watch page and `ytcfg`, which is
  the request that returned 429. That pin was avoiding this surface.
- PR #25 is merged to `main` (9aa5e7b) with CI green. It classifies source-access
  failures (CFR-19), redacts proxy credentials from yt-dlp stdout/stderr before they reach
  the published `ingest.log`, raises the Deno minimum to 2.4.3 to match the pinned BgUtils
  provider, and reads the expected yt-dlp version from the installed distribution. That
  last one also repaired a stale pin in `doctor` that was blocking `launch.cmd`. Both
  version changes are recorded as DEC-012.
- A full spec-versus-repo review produced the remaining confirmed gaps against
  `CLOUD_SPEC.md`: no automatic backoff after an access failure, scratch wiped per claim so
  no retry reuses a partial download (§18.1), and cloud CSV import plus the §10.2 queue
  controls unbuilt.

## Cloud v0.1 boundary

Cloud media remains temporary. PostgreSQL retains job/history metadata after media expiry,
while verified source/Ableton/package files are delivered from private R2 and removed after
the retention window. Source-quality rules remain unchanged.

## Exact next step

1. Confirm Railway redeployed the web and worker services from `main` after PR #25.
2. Check the worker startup log for `YouTube proxy routing is enabled`. If the line is
   absent, `AUDIO_ARCHIVE_YTDLP_PROXY` is not set on the worker and that alone explains
   the 429.
3. If proxy routing is on, check the DataImpulse balance before changing any code.
4. Retry the same Babehoven exact URL with the `ableton` profile. The job page now names
   the failure class (`SourceAccessRateLimited` / `SourceAccessBotCheck` /
   `SourceAccessTokenFailure`), which separates an egress problem from a token problem.
5. If datacenter egress still fails with proxy routing confirmed healthy, take the
   decision `CLOUD_SPEC.md` §9.2 and open decision #9 already anticipate: move acquisition
   to a residential worker. The job model and `worker_network_class` already support it.
