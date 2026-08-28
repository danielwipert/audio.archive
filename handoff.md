# Audio Archive - Session Handoff

**Last updated:** 2026-08-27  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `fix/bgutil-runtime-permissions`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Current verified state

- Cloud v0.1 is deployed on Railway with separate web and worker services, PostgreSQL,
  private Cloudflare R2 temporary delivery, and Cloudflare Access protection.
- PR #21 is merged. The Railway worker routes yt-dlp traffic through the worker-only
  `AUDIO_ARCHIVE_YTDLP_PROXY` secret and redacts the proxy value from persisted command
  metadata.
- A DataImpulse residential proxy is configured in Railway and the worker logs confirm
  proxy routing is enabled.
- The first proxied exact-URL Ableton job completed end to end: browser -> PostgreSQL ->
  worker -> YouTube -> verified source -> 32-bit float Ableton WAV -> SHA-256 -> R2 ->
  signed download.
- The Ableton intermediate is verified `pcm_f32le`, 44.1 kHz stereo, with no resampling,
  normalization, or dither.
- The acquisition was correctly classified `fallback_source`: yt-dlp used combined format
  18 and codec-copy demuxed AAC because the BgUtils PO-token provider failed at runtime.
- The failure evidence is `ERR_INVALID_PACKAGE_CONFIG` plus `Permission denied` while Deno
  reads the provider's `server/node_modules/.deno/.../package.json`. The production image
  installed BgUtils as root and only later switched to UID 10001.
- Branch `fix/bgutil-runtime-permissions` installs the BgUtils provider and Deno dependencies
  as the final `audioarchive` runtime user and adds a CI container check for runtime access to
  the provider dependency tree.

## Cloud v0.1 boundary

Cloud media remains temporary. PostgreSQL retains job/history metadata after media expiry,
while verified source/Ableton/package files are delivered from private R2 and removed after
the retention window. Source-quality rules remain unchanged.

## Exact next step

1. Let CI validate `fix/bgutil-runtime-permissions` and merge its PR to `main`.
2. Confirm Railway redeploys both services from the new production image and the worker is
   Active with proxy routing enabled.
3. Retry the same exact YouTube URL using the Ableton profile.
4. Inspect `archive.json`: the BgUtils permission warnings must be gone and audio-only source
   formats should no longer be skipped for missing GVS PO token.
5. Accept highest-quality acquisition only if the resulting quality status and selection
   evidence support the strongest accessible source under the project policy.
