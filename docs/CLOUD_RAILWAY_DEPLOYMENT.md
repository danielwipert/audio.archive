# Audio Archive Cloud v0.1 — Railway Deployment

This runbook deploys the private Cloud v0.1 architecture defined in `CLOUD_SPEC.md`.
The web and worker are separate Railway services built from the same repository and
Dockerfile. PostgreSQL is the durable control plane. Cloudflare R2 is temporary
private delivery storage.

## Deployment order

1. Provision Railway PostgreSQL.
2. Deploy the web service first. It owns application of `migrations/*.sql`.
3. Confirm `GET /healthz` returns 200.
4. Deploy the worker service. It waits for the web-owned schema before entering its
   polling loop.
5. Configure a Cloudflare-proxied custom hostname and Cloudflare Access.
6. Replace any temporary Access placeholders with the real Access team domain and
   application audience.
7. Run one exact-URL end-to-end acceptance job.

## Shared Railway variables

Set these on both the web and worker services. Use Railway reference variables for
PostgreSQL rather than copying database credentials.

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
R2_ENDPOINT_URL=https://<CLOUDFLARE_ACCOUNT_ID>.r2.cloudflarestorage.com
R2_BUCKET=audio-archive-delivery
R2_ACCESS_KEY_ID=<secret>
R2_SECRET_ACCESS_KEY=<secret>
AUDIO_ARCHIVE_RETENTION_HOURS=24
AUDIO_ARCHIVE_SIGNED_URL_TTL_SECONDS=900
```

Never commit the R2 access key or secret to GitHub.

## Web service

Build source: this repository, root directory `/`, Dockerfile `Dockerfile`.

Default container command:

```text
python -m audio_archive.cloud.runtime web
```

Railway supplies `PORT`; the runtime binds `0.0.0.0:$PORT`.

Additional variables:

```text
CLOUDFLARE_ACCESS_TEAM_DOMAIN=https://<team-name>.cloudflareaccess.com
CLOUDFLARE_ACCESS_AUD=<Access application audience tag>
AUDIO_ARCHIVE_CSRF_SECRET=<random secret, minimum 32 characters>
AUDIO_ARCHIVE_ALLOWED_EMAILS=<comma-separated approved email addresses>
```

Recommended Railway health check:

```text
/healthz
```

The health endpoint intentionally bypasses Cloudflare Access authentication so the
platform can perform origin health checks. All other routes require a valid
`Cf-Access-Jwt-Assertion` and an approved email.

## Worker service

Build source: the same repository, root directory `/`, Dockerfile `Dockerfile`.

Override the Railway start command with:

```text
python -m audio_archive.cloud.runtime worker
```

Additional variables:

```text
AUDIO_ARCHIVE_WORKER_ID=railway-worker-1
AUDIO_ARCHIVE_SUBPROCESS_TIMEOUT_SECONDS=1200
AUDIO_ARCHIVE_WORKER_NETWORK_CLASS=cloud_datacenter
AUDIO_ARCHIVE_SCRATCH_ROOT=/work/jobs
AUDIO_ARCHIVE_WORKER_POLL_SECONDS=2
AUDIO_ARCHIVE_CLEANUP_INTERVAL_SECONDS=300
AUDIO_ARCHIVE_ACCESS_RETRY_LIMIT=3
AUDIO_ARCHIVE_ACCESS_RETRY_BASE_SECONDS=300
AUDIO_ARCHIVE_SCRATCH_RETENTION_HOURS=6
```

A rate-limited, challenged, forbidden or token-failed acquisition is requeued
automatically after 5, 10 and then 20 minutes. Later attempts wait longer because
the condition being waited out is usually a rate-limit window on the worker's
egress path. Set `AUDIO_ARCHIVE_ACCESS_RETRY_LIMIT=0` to disable automatic
requeueing and leave every failure for a manual retry. A source that is
unavailable, private or region-blocked is never retried automatically.

If Railway datacenter egress is blocked by YouTube, configure one worker-only proxy
secret:

```text
AUDIO_ARCHIVE_YTDLP_PROXY=http://<username>:<password>@<proxy-host>:<port>
```

The worker injects this proxy into all yt-dlp calls, including exact-URL acquisition
and artist/title search. The proxy value is redacted from returned command metadata
before ingest logs or job diagnostics can persist the argv. Never commit the proxy
URL or credentials to GitHub, and do not add this variable to the web service.

The worker:

- waits for the PostgreSQL schema created by the web service;
- runs abandoned-job recovery at startup;
- claims one job at a time with renewable PostgreSQL leases;
- uses the existing acquisition and Ableton services;
- polls continuously when the queue is empty;
- cleans expired R2 deliveries periodically; and
- removes each job workspace once its outputs are published.

A failed attempt keeps its workspace so the next attempt can reuse whatever it
verified, which is usually the acquired source master. That saves the download on
a retry, and matters most for long items and for the automatic retries after a
rate-limited acquisition. Only a published archive item is reused, and only after
it re-verifies against its own checksums; job temporary files are always cleared,
because a partial download cannot be distinguished from a complete one by
inspection.

Retained workspaces therefore hold roughly one source master per failed job until
the sweep reclaims them, which it does when the job reaches a terminal state or
after `AUDIO_ARCHIVE_SCRATCH_RETENTION_HOURS`. Lower that value if worker disk is
tight; raise it to keep failed-job diagnostics around for longer.

## Container toolchain

The production Docker image pins/provides:

- Python 3.11;
- the project-pinned yt-dlp and BgUtils Python provider;
- FFmpeg and FFprobe from Debian Bookworm;
- Deno 2.4.3; and
- BgUtils server source 1.3.2 with its Deno dependencies installed.

The BgUtils server is installed under the existing path expected by
`AcquisitionService`: `tools/bgutil-ytdlp-pot-provider/server`.

## Cloudflare R2

Bucket:

```text
audio-archive-delivery
```

Keep it private. Do not enable the public development URL. The application issues
short-lived S3-compatible presigned download URLs after authorization.

Application retention is 24 hours. A bucket lifecycle rule that deletes all objects
after two days is recommended as a secondary safety net.

## First acceptance test

Do not begin with artist/title resolution or CSV. Use one authorized exact YouTube
URL and the Ableton profile.

Acceptance sequence:

1. Open the Access-protected cloud URL.
2. Submit the exact YouTube URL.
3. Confirm the job advances through downloading, verification, conversion, and
   publication.
4. Confirm the source-quality record is present.
5. Download the Ableton WAV.
6. Verify it is readable and 32-bit float PCM at the preserved source sample rate
   and channel layout.
7. Confirm the job exposes its SHA-256 and the R2 object is private outside the
   signed download flow.

A cloud YouTube HTTP 403 or 429 is an infrastructure/network-class failure until
proven otherwise; do not weaken the source-quality rules or silently transcode around
it. If a proxy is configured, retry the same exact-URL job only after the worker has
redeployed and its startup log confirms proxy routing is enabled.
