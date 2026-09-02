# Audio Archive - Session Handoff

**Last updated:** 2026-09-02
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `fix/cloud-youtube-ingestion`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Current verified state

- Cloud v0.1 is deployed on Railway with separate web/worker services, PostgreSQL,
  private R2 temporary delivery, and Cloudflare Access.
- Worker yt-dlp traffic routes through the worker-only `AUDIO_ARCHIVE_YTDLP_PROXY` secret;
  the DataImpulse residential proxy resolves the previous Railway 429/403 egress blocker.
- Proxied exact-URL Ableton jobs complete end to end and produce verified `pcm_f32le` WAVs,
  checksums, R2 publication, and signed downloads.
- PR #22 fixed the BgUtils filesystem-permission failure by installing the provider as the
  final UID 10001 runtime user; that permission error is gone in the latest Railway run.
- PR #23 deployed the BgUtils server 1.3.2 and Deno 2.4.3, but the Python provider remained
  pinned to 1.3.1 and acquisition still forced the `mweb` YouTube client.
- The first production job after PR #23 took about 29.5 minutes and still completed with
  warnings, so the partial provider update did not restore verified audio-only selection.
- Branch `fix/cloud-youtube-ingestion` aligns both provider halves at 1.3.2, updates yt-dlp to
  2026.8.19, lets current yt-dlp select its default YouTube clients, adds bounded network retries,
  and caps each worker subprocess at 20 minutes by default.
- Local verification passes: 113 tests passed and 29 integration tests were skipped because
  their external PostgreSQL/FFmpeg fixtures were unavailable.

## Cloud v0.1 boundary

Cloud media remains temporary. PostgreSQL retains job/history metadata after media expiry,
while verified source/Ableton/package files are delivered from private R2 and removed after
the retention window. Source-quality rules remain unchanged.

## Exact next step

1. Push `fix/cloud-youtube-ingestion`, let CI validate it, and merge its PR to `main`.
2. Confirm Railway redeploys the web and worker and proxy routing remains enabled.
3. Retry the same Babehoven exact URL with the Ableton profile.
4. Inspect `archive.json`: the BotGuard challenge / missing GVS PO-token warnings should be
   gone. Verify whether an audio-only source is selected and whether quality status improves
   from `fallback_source` under the unchanged project policy.
5. Confirm the job finishes within the new bounded execution window.
