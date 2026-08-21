# Audio Archive - Session Handoff

**Last updated:** 2026-08-21  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `main`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Current verified state

- Windows v0.3 automated acceptance passes on two target Windows machines.
- Second-machine run at commit `48a54246db6cea7f0382a7b6914a683fea8a876f`
  passed toolchain readiness, 82 tests, archive verification, and normal/segmented
  Ableton fixture generation.
- Manual Ableton Live 12 acceptance passed for both the normal 32-bit float WAV
  and the segmented fixture set.
- First authorized live YouTube acquisition reached metadata extraction and chose
  format 251, then failed at the media request with HTTP 403.
- The failure matched current YouTube GVS PO-token enforcement behavior.
- PR #13 added project-managed BgUtils PO-token support, pins plugin/server 1.3.1,
  uses the recommended `mweb` client for acquisition, and was squash-merged into
  `main` as commit `d55bbaa5ad9e081c0f4e5279f89079e481e7a0c5`.
- PR #13 passed Linux installation/lint/compile/PowerShell validation/tests and
  Windows Python 3.14 installation/PowerShell validation/compile/tests before merge.

## Project-use decision

Permanent archive use remains blocked until one authorized live complete-profile
YouTube acquisition passes with the PO-token path and the remaining browser/manual
acceptance checks are verified.

## Exact next step

1. On the target Windows machine, run `git pull origin main`.
2. Run `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` to install the
   pinned BgUtils plugin/server and Deno dependencies.
3. Rerun the same authorized YouTube item through the complete acceptance command.
4. If live ingestion passes, inspect the resulting master, Ableton WAV, MP3,
   metadata/artwork/logs/manifest/checksums and finish the remaining browser checks
   (restart recovery, CSV mixed-row handling, ambiguous-source review).
