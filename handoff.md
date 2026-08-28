# Audio Archive - Session Handoff

**Last updated:** 2026-08-27  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `main`

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
- That acquisition was correctly classified `fallback_source`: yt-dlp used combined format
  18 and codec-copy demuxed AAC because the BgUtils PO-token provider failed at runtime.
- The failure evidence was `ERR_INVALID_PACKAGE_CONFIG` plus `Permission denied` while Deno
  read the provider's `server/node_modules/.deno/.../package.json`.
- PR #22 is merged. The production image now creates the `audioarchive` runtime user before
  installing BgUtils and installs the provider/Deno dependency tree as UID 10001.
- CI run #51 passed, including a container-level regression check that runs as UID 10001 and
  verifies access to the BgUtils dependency tree.

## Cloud v0.1 boundary

Cloud media remains temporary. PostgreSQL retains job/history metadata after media expiry,
while verified source/Ableton/package files are delivered from private R2 and removed after
the retention window. Source-quality rules remain unchanged.

## Exact next step

1. Confirm Railway redeploys the worker from merged `main` and becomes Active with proxy
   routing enabled.
2. Retry the same exact YouTube URL using the Ableton profile.
3. Inspect the new `archive.json`: the BgUtils permission warnings must be gone and audio-only
   source formats should no longer be skipped because of this runtime-permission failure.
4. Accept highest-quality acquisition only if the resulting quality status and selection
   evidence support the strongest accessible source under the project policy.
