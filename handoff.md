# Audio Archive - Session Handoff

**Last updated:** 2026-08-23  
**Repository:** `danielwipert/audio.archive`  
**Working branch:** `main`

## Session protocol

Read this file first. At session end, overwrite it with the current verified
state and exact next step. Keep it short; this is not a cumulative changelog.

## Current verified state

- Local Windows v0.3 remains the audio-quality reference implementation for source
  acquisition, PO-token handling, FFprobe verification, checksums, Ableton conversion,
  and long-form segmentation.
- `CLOUD_SPEC.md` defines Cloud v0.1 as a private browser-accessible service with
  temporary downloadable outputs rather than permanent cloud media retention.
- Cloud deployment decisions are locked: Railway web + worker, Railway PostgreSQL,
  Cloudflare R2 temporary delivery, and Cloudflare Access authentication.
- Cloud processing state and delivery lifecycle are separated; default delivery
  retention is 24 hours and signed-download URL lifetime is 15 minutes.
- PR #16 is merged as commit `6f2428e77ea412d089c54c7565a49a5baf9516e6`.
  It adds private R2 publication, verified object metadata, presigned downloads,
  database-authoritative expiry, and cleanup reconciliation.
- PR #17 is merged as commit `58378586d1fb9112c4c41a9115aa487fadedbfa7`.
  It adds claim-specific ephemeral workspaces, PostgreSQL processing-attempt persistence,
  worker lease heartbeats and abandoned-job recovery, and cloud orchestration around the
  existing local acquisition and Ableton services.
- Cloud profiles `source`, `ableton`, and `package` publish only verified outputs; partial
  publication stays inaccessible and is rolled back best-effort.
- Published preservation manifests retain the cloud profile requested by the user while
  the adapter may use local service profile aliases internally.
- PR #17 passed Linux/PostgreSQL CI, Ruff/compile/script checks, and Windows Python 3.14 CI
  before merge.

## Cloud v0.1 boundary

Cloud media is temporary in v0.1. PostgreSQL retains job/history metadata after
media expiry, while verified source/Ableton/package files are delivered from private
R2 and removed after the retention window. The cloud worker reuses the proven local
quality-critical services rather than implementing a second audio pipeline.

## Exact next step

1. Create the cloud web/API block.
2. Add FastAPI job submission for exact URL and artist/title requests using PostgreSQL.
3. Add job detail/status/history and output listing endpoints.
4. Add candidate-review endpoints that can approve a candidate, accept a replacement URL,
   or mark a job not found while preserving resolver evidence.
5. Add signed-download authorization that returns a short-lived URL only for currently
   downloadable outputs.
6. Add the minimal authenticated browser UI for submit, queue/status, review, and download.
7. Treat Cloudflare Access as the external authentication boundary and reject requests
   that do not carry the expected Access identity headers in production mode.
8. Add Railway web and worker process entrypoints, startup migrations/recovery, and periodic
   delivery-expiry cleanup.
9. Cover the API/auth/review/download lifecycle with deterministic and PostgreSQL-backed tests,
   then deploy a private Cloud v0.1 staging instance.
