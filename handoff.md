# Audio Archive - Session Handoff

**Last updated:** 2026-08-27  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `fix/bgutil-1.3.2`

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
- The latest run still classified `fallback_source`. New evidence is a BgUtils token-generation
  failure: `Could not get BotGuard challenge`, followed by missing mweb GVS PO token and skipped
  audio-only formats.
- Production is still pinned to BgUtils 1.3.1 and Deno 2.3.7.
- Upstream BgUtils 1.3.2 was released 2026-08-21 specifically to mint WebPO tokens from the
  homepage challenge + ytcfg and mitigate failures affecting 1.3.1. BgUtils 1.3.2 requires
  Deno >= 2.4.3 for the script provider.
- Branch `fix/bgutil-1.3.2` upgrades BgUtils to 1.3.2 and Deno to 2.4.3 and extends CI to
  validate the pinned versions, runtime permissions, and script `--version` execution.

## Cloud v0.1 boundary

Cloud media remains temporary. PostgreSQL retains job/history metadata after media expiry,
while verified source/Ableton/package files are delivered from private R2 and removed after
the retention window. Source-quality rules remain unchanged.

## Exact next step

1. Let CI validate `fix/bgutil-1.3.2` and merge its PR to `main`.
2. Confirm Railway redeploys the worker and proxy routing remains enabled.
3. Retry the same Babehoven exact URL with the Ableton profile.
4. Inspect `archive.json`: the BotGuard challenge / missing GVS PO-token warnings should be
   gone. Verify whether an audio-only source is selected and whether quality status improves
   from `fallback_source` under the unchanged project policy.
5. Separately add a bounded yt-dlp subprocess timeout so a stalled network/provider call
   cannot hold the sequential cloud worker indefinitely.
