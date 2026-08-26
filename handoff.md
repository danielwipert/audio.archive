# Audio Archive - Session Handoff

**Last updated:** 2026-08-26  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `cloud/07-ytdlp-proxy`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Current verified state

- Local Windows v0.3 remains the audio-quality reference implementation for native
  YouTube acquisition, FFprobe verification, checksums, Ableton 32-bit float WAV
  conversion, and long-form segmentation.
- Cloud v0.1 is deployed on Railway with separate web and worker services plus Railway
  PostgreSQL. The worker is private/unexposed and the web service is publicly routed only
  through the protected application hostname.
- Cloudflare R2 bucket `audio-archive-delivery` is private, has temporary retention safety
  rules, and is configured for signed download delivery.
- Cloudflare Access is active in front of the cloud web app and application-side JWT/email
  verification is working.
- `GET /healthz` returns 200 through the custom Cloudflare hostname.
- PR #20 is merged and provides the production Docker image, Railway web/worker runtime,
  migrations, worker recovery/polling, and deployment runbook.
- The first exact-URL Ableton acceptance job successfully traversed browser submission,
  PostgreSQL persistence, worker claim, and yt-dlp execution, then failed at native source
  acquisition because YouTube returned HTTP 429 / "Sign in to confirm you're not a bot".
- This is classified as cloud egress/network reputation failure, not an audio-quality or
  conversion failure. Do not weaken source-quality rules or silently transcode around it.
- Branch `cloud/07-ytdlp-proxy` adds optional worker-only yt-dlp proxy routing using
  `AUDIO_ARCHIVE_YTDLP_PROXY`. It applies to exact URL and artist/title yt-dlp calls and
  redacts proxy credentials from command metadata before logs/diagnostics persist argv.

## Cloud v0.1 boundary

Cloud media remains temporary. PostgreSQL retains job/history metadata after media expiry,
while verified source/Ableton/package files are delivered from private R2 and removed after
the retention window. The cloud worker reuses the proven local quality-critical services.

## Exact next step

1. Let CI validate `cloud/07-ytdlp-proxy` and merge its PR to `main`.
2. Obtain one suitable US proxy endpoint for authorized YouTube acquisition.
3. Add the complete proxy URL only to the Railway worker as the secret variable
   `AUDIO_ARCHIVE_YTDLP_PROXY`; do not add it to the web service or GitHub.
4. Confirm the worker redeploys Active and logs only that proxy routing is enabled, never
   the proxy value.
5. Retry the same exact-URL Ableton acceptance job.
6. If acquisition succeeds, verify native source provenance, Ableton 32-bit float PCM,
   SHA-256 values, private R2 publication, signed download, and cleanup behavior.
7. If YouTube still returns 403/429, treat the proxy endpoint itself as the remaining
   infrastructure variable and do not change the audio pipeline.
