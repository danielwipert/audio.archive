# Audio Archive - Session Handoff

**Last updated:** 2026-09-02
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `claude/project-review-spec-74pzyw`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Current verified state

- Cloud v0.1 is deployed on Railway with separate web/worker services, PostgreSQL,
  private R2 temporary delivery, and Cloudflare Access.
- PR #24 is merged to `main` (752f3d2). It aligned both BgUtils halves at 1.3.2, updated
  yt-dlp to 2026.8.19, removed the forced `mweb` YouTube client, added bounded network
  retries, and capped each worker subprocess at 20 minutes.
- The first production job after PR #24 (exact URL, `ableton` profile) failed at
  `downloading` 13 seconds in, with HTTP 429 on the webpage fetch plus
  `Sign in to confirm you're not a bot`. No media transferred. Whether
  `AUDIO_ARCHIVE_YTDLP_PROXY` survived the redeploy has not been confirmed.
- Removing the `mweb` pin means yt-dlp now fetches the watch page and `ytcfg`, which is
  the request that returned 429. That pin was avoiding this surface.
- A full spec-versus-repo review was completed. Confirmed gaps against `CLOUD_SPEC.md`:
  source-access failures are not classified (CFR-19); proxy redaction covers argv but not
  yt-dlp stdout/stderr, which is published in `ingest.log`; cloud CSV import and the
  §10.2 queue controls are unbuilt; scratch is wiped per claim so no retry reuses it.
- PR #24 also left `doctor` comparing the yt-dlp executable against a hardcoded 2026.7.4,
  so the readiness check fails against the new pin and `launch.cmd` refuses to start.
- Local verification on this branch: 121 tests passed, 32 skipped because external
  PostgreSQL and FFmpeg fixtures were unavailable.

## Cloud v0.1 boundary

Cloud media remains temporary. PostgreSQL retains job/history metadata after media expiry,
while verified source/Ableton/package files are delivered from private R2 and removed after
the retention window. Source-quality rules remain unchanged.

## Exact next step

1. Merge `claude/project-review-spec-74pzyw`: source-access error classes, proxy
   redaction of tool output, Deno minimum raised to 2.4.3, and the `doctor` yt-dlp pin
   read from the installed distribution instead of a stale constant (DEC-012).
2. Check the Railway worker startup log for `YouTube proxy routing is enabled`. If the
   line is absent, `AUDIO_ARCHIVE_YTDLP_PROXY` is not set on the worker and that alone
   explains the 429.
3. If proxy routing is on, check the DataImpulse balance before changing any code.
4. Retry the same Babehoven exact URL with the `ableton` profile. The job page should now
   name the failure class (`SourceAccessRateLimited` / `SourceAccessBotCheck`) instead of
   `ToolExecutionError`.
5. If datacenter egress still fails with proxy routing confirmed healthy, take the
   decision `CLOUD_SPEC.md` §9.2 and open decision #9 already anticipate: move acquisition
   to a residential worker. The job model and `worker_network_class` already support it.
