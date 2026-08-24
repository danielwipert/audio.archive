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
- PostgreSQL schema, migrations, cloud environment configuration, processing/delivery
  models, job creation, state transitions, and transaction-safe worker leases are on main.
- PR #16 is merged as commit `6f2428e77ea412d089c54c7565a49a5baf9516e6`.
  It adds private R2 publication, verified object metadata, presigned downloads,
  database-authoritative expiry, and cleanup reconciliation.
- PR #16 passed Linux/PostgreSQL CI and Windows Python 3.14 CI before merge.

## Cloud v0.1 boundary

Cloud media is temporary in v0.1. PostgreSQL retains job/history metadata after
media expiry, while verified source/Ableton/package files are delivered from private
R2 and removed after the retention window. The existing local audio pipeline must be
wrapped or cleanly extracted; its audio behavior must not be independently reimplemented.

## Exact next step

1. Create the cloud worker/pipeline integration block.
2. Add a job-isolated ephemeral scratch workspace for each cloud processing attempt.
3. Adapt the proven local acquisition and Ableton services away from local SQLite /
   permanent archive assumptions without changing source-quality or conversion policy.
4. Drive PostgreSQL processing transitions from the cloud worker and preserve worker
   claim/heartbeat semantics.
5. Publish only verified requested outputs through `TemporaryDeliveryService`, then
   finalize processing and delivery state.
6. Add deterministic/integration tests for worker interruption, publication failure,
   source-only output, and Ableton output.
7. After that block passes CI, expose cloud job submission/status/download authorization
   through the FastAPI application.
