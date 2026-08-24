# Audio Archive Cloud v0.1 — Architecture Decisions

**Status:** Locked for initial implementation  
**Date:** 2026-08-23  
**Specification:** `CLOUD_SPEC.md`

## Deployment stack

Cloud v0.1 will begin with:

- **Railway** for the FastAPI web service.
- **Railway** for the background worker service.
- **Railway PostgreSQL** for persistent application and queue state.
- **Cloudflare R2** for private temporary delivery objects.
- **Cloudflare R2 object lifecycle rules** as the primary expiry mechanism.
- An application cleanup/reconciliation job as a secondary deletion safeguard.

The web and worker services are separate deployment processes even when they share the same repository and deployment project.

## Queue decision

Cloud v0.1 will use PostgreSQL as the queue and state store. Redis, RabbitMQ, Celery, and a separate broker are deliberately deferred.

The initial system has one sequential worker, so the worker will claim work atomically from PostgreSQL. The claim implementation must use a transaction-safe pattern equivalent to `SELECT ... FOR UPDATE SKIP LOCKED` and must record worker identity, claim time, and lease/heartbeat information.

A dedicated broker may be introduced only if later concurrency, scheduling, or throughput requirements justify the added operational component.

## Processing state versus delivery lifecycle

Cloud state is intentionally split into two dimensions.

### Processing state

Processing state describes what happened to the job:

- `pending`
- `resolving`
- `needs_review`
- `ready`
- `downloading`
- `verifying_master`
- `converting`
- `verifying_output`
- `packaging`
- `publishing`
- `completed`
- `completed_with_warnings`
- `failed`
- `interrupted`
- `skipped_duplicate`
- `not_found`
- `cancelled`

### Delivery lifecycle

Delivery lifecycle describes whether verified media is currently downloadable:

- `not_published`
- `available`
- `deletion_pending`
- `expired`
- `deleted`

The UI may derive labels such as `ready_to_download` from a successful processing state plus `available`, and `files_expired` from an expired/deleted delivery state. This avoids corrupting historical processing results when temporary media expires.

## Retention

- Default delivery retention: **24 hours after successful publication**.
- Default signed-download URL lifetime: **15 minutes**.
- R2 lifecycle expiry is the authoritative storage-level backstop.
- Database expiry timestamps are authoritative for application behavior.
- The application must stop issuing download links once its recorded expiry time is reached, even if R2 lifecycle deletion has not physically completed yet.
- Cleanup reconciliation verifies that expired objects are actually gone and records deletion status.

## Storage boundaries

PostgreSQL stores metadata and state only. It must not contain audio blobs.

Worker scratch storage is ephemeral and job-isolated. It is used for acquisition, verification, conversion, packaging, and upload, then removed after successful publication.

R2 stores only temporary delivery objects for Cloud v0.1. Permanent source retention remains out of scope.

## Worker portability

The worker protocol must not assume a Railway network path or a Linux-only execution environment. The initial Railway worker is an implementation choice, not an architectural dependency.

Every processing attempt records:

- worker ID
- worker network class (`cloud_datacenter`, `residential`, or `unknown`)
- start/end timestamps
- result
- source-access error class when applicable

This preserves the ability to move YouTube acquisition to a residential worker without changing the browser workflow or source-resolution decision.

## Authentication

Cloud v0.1 will use **Cloudflare Access** in front of the private application.

Initial policy:

- self-hosted Access application protecting the full Audio Archive hostname
- explicit approved-email allowlist
- email one-time PIN as the initial identity method
- no anonymous or bypass policy for application pages or APIs
- health checks may be exposed separately only when they reveal no sensitive state

The FastAPI origin must validate the signed `Cf-Access-Jwt-Assertion` token and its audience rather than trusting the presence of an Access header. The origin must not treat an unverified email header as authentication.

The Railway-provided service hostname must not become an alternate unprotected application entry point. Production traffic should reach the application through the Access-protected hostname, with origin exposure constrained during deployment.

This keeps authentication external to the application while preserving an application-level verification boundary.

## Compatibility rule

The existing local v0.3 pipeline remains the source of truth for resolver behavior, source-master quality, FFprobe verification, SHA-256 integrity, Ableton conversion, and long-form segmentation until equivalent cloud acceptance tests pass.

Cloud work must wrap or extract those proven components rather than reimplementing their audio behavior independently.
