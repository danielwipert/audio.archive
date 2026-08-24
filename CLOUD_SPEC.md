# Audio Archive — Cloud Ingestion Project Specification

**Version:** 0.1 (first cloud draft)  
**Status:** Draft for approval  
**Date:** 2026-08-23  
**Primary client:** Modern web browser  
**Primary editing environment:** Ableton Live  
**Repository:** `danielwipert/audio.archive`  
**Parent specification:** `Audio Archive — YouTube Ingestion Project Specification v0.3`

## 1. Purpose

Build a private, cloud-accessible version of Audio Archive that can be used from any modern browser to find and ingest authorized audio from YouTube, preserve the best accessible source during processing, create an Ableton-ready working file without unnecessary quality loss, and deliver the finished files to the user for download.

The first cloud release is intentionally **not a permanent cloud audio archive**. It is a remote processing and delivery system:

```text
Submit from anywhere
        ↓
Resolve intended source
        ↓
Acquire best accessible native audio
        ↓
Verify source
        ↓
Create requested output
        ↓
Verify output
        ↓
Publish temporary download
        ↓
User downloads files
        ↓
Automatic cloud cleanup
```

The first cloud release retains the three primary input paths defined by the local v0.3 specification:

1. Artist and song title
2. Exact YouTube URL
3. CSV batch of artists and songs

The system remains intended only for material the user owns or is authorized to download and archive.

## 2. Relationship to the local v0.3 specification

This specification extends the local Audio Archive v0.3 design rather than replacing its audio-quality rules.

The following v0.3 principles remain authoritative unless explicitly changed here:

- Resolution and acquisition are separate decisions.
- The approved YouTube source must be pinned before acquisition.
- The best accessible native audio stream is the canonical acquisition source.
- The source master must not be transcoded, normalized, resampled, or otherwise processed during acquisition.
- Ableton output is created locally from the acquired source master.
- The default Ableton intermediate is 32-bit float PCM WAV at the decoded source sample rate and channel count.
- FFprobe verification and SHA-256 integrity checks remain required.
- yt-dlp source metadata and project-controlled provenance must remain separate.
- Manual entry, exact URL, and CSV rows must create the same internal job type and use the same pipeline.
- Quality warnings must change the reported acquisition status rather than being hidden.

The principal architectural change is storage and execution location:

```text
v0.3 local

Browser on Windows
      ↓
Local FastAPI
      ↓
SQLite queue
      ↓
Local ingestion worker
      ↓
Persistent local archive
```

becomes:

```text
Cloud v0.1

Browser anywhere
      ↓
Authenticated cloud web application
      ↓
Persistent cloud job state
      ↓
Ingestion / processing worker
      ↓
Temporary processing storage
      ↓
Temporary downloadable assets
      ↓
Automatic deletion
```

## 3. Product principle

The cloud service should behave like a **private audio-processing appliance**, not a public media-downloading site and not yet a permanent cloud media library.

The user should be able to submit work remotely and retrieve verified finished files without managing:

- yt-dlp commands
- codecs or containers
- FFmpeg arguments
- JavaScript runtimes
- PO Token implementation details
- checksums
- temporary storage
- cloud object storage
- server directories
- worker processes

The first cloud release prioritizes:

1. Access from anywhere.
2. Highest practical source quality under the existing Audio Archive rules.
3. Dependable processing and verification.
4. Simple downloads.
5. Automatic cleanup.
6. Low permanent cloud-storage cost.
7. An architecture that can later support permanent archiving without requiring a rewrite.

## 4. First-release scope

The first cloud release will provide:

- Private authenticated web access.
- Artist + title submission.
- Exact YouTube URL submission.
- CSV batch import.
- Candidate resolution and manual review.
- Persistent job queue and job history.
- Native source acquisition.
- Source verification.
- Ableton 32-bit float WAV creation.
- Long-form segmentation when required by the existing size policy.
- Optional native-source download.
- Optional archive-package download containing preservation metadata.
- Temporary file delivery through expiring download links.
- Automatic cleanup after the retention period.
- Logs and diagnostic status sufficient to understand failures.

The first release does **not** require permanent retention of source masters, WAV files, thumbnails, or archive packages after the temporary download window expires.

## 5. Target user experience

### 5.1 Access

The user visits the private Audio Archive URL from a browser on a desktop, laptop, tablet, or phone.

Example:

```text
https://audio.example.com
```

The user authenticates before seeing the application.

The application restores the current queue and recent job history after login.

### 5.2 Single-item workflow

```text
Enter artist + song
        or
Paste exact YouTube URL
        ↓
Find and Rip
        ↓
Resolve source
        ↓
Auto-approve strong match
or request review
        ↓
Acquire source
        ↓
Verify source
        ↓
Create Ableton WAV
        ↓
Verify WAV
        ↓
Ready to Download
```

The single-item form contains:

- Artist
- Song title
- Optional version qualifier
- Optional exact YouTube URL
- Output profile
- `Find and Rip` action

The default output profile remains `ableton`.

### 5.3 CSV workflow

The CSV schema and validation behavior remain compatible with the local v0.3 specification.

Minimum CSV:

```csv
artist,title
Massive Attack,Teardrop
Portishead,Roads
Radiohead,Everything in Its Right Place
```

Optional fields:

```csv
artist,title,version,url,profile
Massive Attack,Teardrop,album,,ableton
Portishead,Roads,live,https://www.youtube.com/watch?v=EXAMPLE,ableton
```

Accepted rows become independent persistent jobs. Rejected rows are reported without blocking valid rows.

### 5.4 Completed-job experience

A completed job displays at least:

```text
PORTISHEAD — ROADS

Status: Ready to Download
Quality: verified_best_available

Source
Opus / 48 kHz / Stereo

Ableton
32-bit Float WAV / 48 kHz / Stereo

[ Download Ableton WAV ]
[ Download Source Master ]
[ Download Archive Package ]

Files expire: <timestamp>
```

The interface must show the exact expiration time of temporary files.

## 6. Storage model

### 6.1 Storage principle

Cloud v0.1 separates four different concepts:

1. **Job state** — small persistent records kept in the application database.
2. **Worker scratch storage** — temporary local disk used while acquisition and conversion are actively running.
3. **Delivery storage** — temporary object storage used to serve completed files for download.
4. **Permanent archive storage** — deferred to a future version.

The absence of permanent cloud audio retention does not eliminate provenance. Small job-history records remain after the media files expire.

### 6.2 Worker scratch storage

Each active job receives an isolated temporary workspace:

```text
/work/jobs/<job_id>/

    source.<native_ext>
    source.info.json
    source-thumbnail.<ext>
    ableton.wav
    archive.json
    SHA256SUMS
    logs/
```

Scratch storage is not an archive.

Rules:

- Every job has its own directory.
- Partial files must never be exposed as completed downloads.
- Scratch paths must not be user-controlled.
- Scratch files are removed after successful publication to delivery storage.
- Failed-job scratch files may be retained briefly for retry or diagnostics, then deleted automatically.
- Worker restart recovery must distinguish reusable completed assets from partial files.

### 6.3 Temporary delivery storage

After requested outputs pass verification, the system publishes them to private temporary object storage.

Conceptual layout:

```text
delivery/
  <job_id>/
    source/
      <video_id>.<native_ext>
    ableton/
      <video_id>.wav
    archive/
      <video_id>-archive.zip
```

Object storage must remain private. Files are downloaded through short-lived signed URLs or an equivalent authenticated delivery mechanism.

No object may be publicly readable by default.

### 6.4 Default retention window

The initial default retention period is:

> **24 hours after successful publication.**

The application records:

- `published_at_utc`
- `expires_at_utc`
- deletion status

The user interface must display the expiration time.

A future configuration may support alternative retention periods such as 6, 24, 48, or 72 hours, but v0.1 requires one documented system default.

### 6.5 Automatic deletion

Expired delivery objects must be deleted automatically by storage lifecycle policy whenever the storage provider supports it.

Application cleanup provides a secondary safeguard.

The system must not depend solely on a best-effort application timer for deletion.

After successful expiry cleanup:

- Media assets are deleted.
- Archive ZIP files are deleted.
- Temporary artwork and source metadata files are deleted from object storage.
- Scratch storage is deleted.
- Small database history remains.

### 6.6 Persistent job history

The database may retain lightweight history such as:

```text
Artist: Portishead
Title: Roads
YouTube ID: VIDEO_ID
Processed: 2026-08-23
Resolution: automatic
Acquisition quality: verified_best_available
Selected source codec: opus
Selected source sample rate: 48000
Ableton output: completed
Download window: expired
Files retained: no
```

The database must not claim that an expired file remains archived.

The distinction between `completed` and `files_expired` must be visible in system state.

## 7. Download outputs

### 7.1 Default Ableton download

The primary output is the Ableton intermediate defined by v0.3:

| Property | Required value |
| --- | --- |
| Container | WAV |
| Audio format | 32-bit float PCM (`pcm_f32le`) |
| Sample rate | Same as decoded source |
| Channels | Same as source, limited to mono or stereo |
| Normalization | Off |
| Resampling | None by default |
| Dither | None |
| Source | Verified native source master |

### 7.2 Native source download

The user may download the verified native source master before expiry.

The file must retain the codec and encoded audio packets acquired from the selected YouTube format, subject to any codec-copy demux fallback defined by v0.3.

### 7.3 Archive package

The user may request a downloadable ZIP package containing preservation material.

Initial contents:

```text
<video_id>-archive/
  master/
    <video_id>.<native_ext>
  intermediates/
    ableton/
      <video_id>.wav
  artwork/
    source-thumbnail.<ext>
  metadata/
    source.info.json
    archive.json
  checksums/
    SHA256SUMS
```

If the Ableton output is segmented, the package contains the ordered segment set and segment metadata.

The ZIP package is a delivery convenience. It is not itself the canonical integrity record; `SHA256SUMS` and `archive.json` remain authoritative inside it.

### 7.4 Download behavior

The system should prefer direct browser download from private object storage using an expiring signed URL.

Requirements:

- HTTPS only.
- URL expires after a short configurable period.
- Authentication is required to request a new signed URL.
- Expired delivery objects cannot be restored without reprocessing unless a future permanent-source feature exists.
- The application must not proxy multi-gigabyte files through the web application process when direct object-storage delivery is available.

## 8. Lean cloud architecture

### 8.1 Logical architecture

```text
Browser
   ↓ HTTPS
Authentication
   ↓
Cloud Web Application
   ├── Input validation
   ├── Candidate review
   ├── Job API
   ├── Queue UI
   └── Download authorization
          │
          ├──────────────→ PostgreSQL
          │                   job state
          │                   candidates
          │                   events
          │                   history
          │
          ↓
       Job Queue
          ↓
    Processing Worker
      ├── resolver
      ├── yt-dlp
      ├── Deno / JS runtime
      ├── PO-token integration when required
      ├── FFmpeg
      ├── FFprobe
      ├── verification
      └── packaging
          │
          ├── temporary scratch disk
          │
          ↓
    Private Object Storage
          ↓
    Expiring Download URL
          ↓
        Browser
```

### 8.2 Required components

Cloud v0.1 requires:

- Python application core
- FastAPI web application
- Server-rendered HTML or an equally lean web presentation layer
- Minimal browser JavaScript
- PostgreSQL
- Persistent job queue
- One sequential processing worker initially
- yt-dlp
- FFmpeg
- FFprobe
- Supported JavaScript runtime
- Supported PO Token provider integration when required by current YouTube behavior
- Private object storage
- HTTPS termination
- Authentication
- Secrets management
- Automated deployment
- Structured application and worker logs

### 8.3 One worker initially

The first cloud release uses one active acquisition/conversion worker unless testing proves concurrency is necessary.

This preserves the local v0.3 principle of simple sequential processing and reduces:

- rate-limit pressure
- concurrent disk usage
- temporary-storage spikes
- YouTube anti-abuse exposure
- state complexity
- duplicate processing

Jobs awaiting manual candidate review do not block ready jobs.

### 8.4 Logical worker separation

The worker must be logically independent from the web process even if v0.1 initially deploys both on the same host or service group.

The web application must never require the browser request itself to remain open while processing occurs.

A submitted job continues when the browser is closed.

## 9. Worker portability and network risk

### 9.1 Worker abstraction

The ingestion worker is a replaceable execution node.

The queue communicates with the worker through application-controlled job state rather than assuming a specific machine path.

This permits future worker types such as:

- cloud Linux worker
- Windows worker
- macOS worker
- residential-network worker
- NAS worker
- dedicated conversion worker

### 9.2 YouTube cloud-egress risk

YouTube acquisition from data-center networks is a known operational risk and must be treated as an infrastructure constraint rather than an application-level success assumption.

The implementation must therefore:

- Classify HTTP 403 and source-access failures separately from conversion failures.
- Record the worker identity and network class with an acquisition attempt where practical.
- Avoid declaring a source unavailable solely because one worker/network path failed.
- Permit a future alternate worker to retry the pinned source without rerunning candidate resolution.
- Keep the control plane independent from the acquisition worker location.

Cloud v0.1 may begin with a cloud-hosted worker, but the architecture must not require cloud-hosted acquisition permanently.

### 9.3 Current YouTube token requirements

The worker toolchain must support the current yt-dlp-recommended YouTube extraction path, including a PO Token provider plugin when required.

Token generation and token refresh are runtime infrastructure concerns and must not alter archive metadata or audio content.

Credentials, cookies, visitor/session values, and generated token material must never be placed in:

- Git
- `archive.json`
- `source.info.json` modifications
- CSV provenance
- downloadable archive packages
- normal application logs

## 10. Persistent job queue

### 10.1 Core job states

Cloud v0.1 retains the local pipeline states and adds delivery lifecycle states where needed.

| State | Meaning |
| --- | --- |
| `pending` | Validated and waiting for processing |
| `resolving` | Searching and scoring candidates |
| `needs_review` | Source selection requires user input |
| `ready` | Source is pinned and acquisition may begin |
| `downloading` | Native source is being acquired |
| `verifying_master` | Source master is being inspected and checksummed |
| `converting` | Requested derivative is being created |
| `verifying_output` | Created output is being inspected and checksummed |
| `packaging` | Optional archive package is being created |
| `publishing` | Verified outputs are being copied to delivery storage |
| `ready_to_download` | Required files are published and available |
| `completed_with_warnings` | Processing succeeded but quality warnings exist |
| `failed` | Processing stopped with a recorded error |
| `interrupted` | Worker stopped during an active stage |
| `skipped_duplicate` | Existing reusable asset satisfied the job within the active processing context |
| `not_found` | No source was approved |
| `cancelled` | Work was cancelled before completion |
| `files_expired` | Processing history remains but temporary files were deleted |

A job's processing result and file-retention state may be represented by separate fields internally if this prevents ambiguous transitions.

### 10.2 Queue behavior

The interface must provide:

- Start processing automatically after valid submission unless paused globally.
- Pause after current job.
- Resume queue.
- Retry failed job.
- Cancel pending job.
- Review ambiguous job.
- Regenerate a fresh signed download URL while files still exist.
- Delete temporary files early.

### 10.3 Browser independence

Once a job has been accepted into persistent state, processing must not depend on the originating browser session remaining connected.

The user may:

1. Submit a job from a phone.
2. Close the browser.
3. Return later from another device.
4. See current status.
5. Download the completed file if it has not expired.

## 11. Source resolution

The resolver behavior remains consistent with v0.3.

For artist/title input, the system will:

1. Normalize requested metadata while preserving original input.
2. Search a bounded set of YouTube candidates.
3. Store candidate evidence.
4. Score candidates deterministically.
5. Auto-select only above the documented confidence threshold.
6. Otherwise require user review.

The initial scoring policy remains:

- Best score at least 90.
- Lead over runner-up at least 15 points.
- No disqualifying unrequested-version term.
- Conflicts route to review.

Exact URL jobs bypass search and pin the supplied source.

Manual review remains available from any authenticated browser.

## 12. Quality principles

Cloud deployment does not lower the v0.3 quality standard.

For this project, highest quality remains:

> The highest-ranked, preferably non-DRC audio stream that yt-dlp can access for the approved YouTube item and acquisition session, preserved without transcoding, resampling, normalization, or other signal processing.

The system must not claim access to an original studio master or lossless pre-upload source.

### 12.1 Source master

The processing worker must:

- Select the best accessible approved format under controlled format-selection policy.
- Prefer non-DRC where the extractor exposes the distinction.
- Avoid audio transcoding.
- Avoid automatic normalization and filtering.
- Verify the resulting stream.
- Generate SHA-256.
- Record selection evidence and warnings.

### 12.2 Ableton intermediate

The Ableton intermediate must be created from the verified local source master in the job workspace, never by downloading another source independently.

Required default:

- WAV
- `pcm_f32le`
- decoded source sample rate
- decoded source channels
- no normalization
- no resampling by default
- no dither
- no audio filters

### 12.3 Long-form policy

The v0.3 long-form size policy remains in effect.

Initial safe threshold:

> 1.8 GiB

Default segment duration:

> 60 minutes

Above the threshold, the system creates ordered, gapless 32-bit float WAV segments and records segment metadata and checksums.

## 13. Quality status

The acquisition statuses remain:

| Status | Meaning |
| --- | --- |
| `verified_best_available` | Preferred non-DRC audio-only stream selected with no quality-limiting warnings |
| `best_available_with_warnings` | Usable stream preserved but access conditions may have limited the available formats |
| `fallback_source` | Preferred audio-only stream was unavailable and a lower-ranked or combined source was used |
| `failed` | No valid source master was created or verification failed |

Cloud/network-specific access restrictions must feed these statuses or an associated warning field rather than being hidden.

## 14. Controlled format selection

The ingestion engine must continue to:

1. Ignore unrelated global yt-dlp configuration.
2. Request best accessible audio-only first.
3. Permit controlled combined-stream fallback only when audio-only is unavailable.
4. Preserve extractor quality ranking instead of selecting solely by nominal bitrate or extension.
5. Prefer non-DRC over DRC where available.
6. Record available-format evidence.
7. Avoid `--extract-audio`, `--audio-format`, or another option that would re-encode the source master.
8. Capture JavaScript-runtime, token, authentication, format, and 403 warnings.
9. Use the approved JavaScript / PO-token integration without leaking secrets.

The exact production command is an implementation detail and may change as YouTube and yt-dlp change, but the quality policy above is stable.

## 15. Database model

### 15.1 PostgreSQL role

PostgreSQL replaces SQLite for the cloud deployment because state must be safely shared between the web application and worker processes.

PostgreSQL stores:

- users or authorized identities
- jobs
- job state
- job events
- CSV import provenance
- candidate sets
- resolver scores
- approved source IDs
- processing attempts
- worker identity
- quality warnings
- output records
- temporary object keys
- publication timestamps
- expiration timestamps
- deletion status
- concise failure summaries

### 15.2 Persistent data versus temporary media

PostgreSQL must not contain audio blobs.

Media files live only in:

- temporary worker scratch storage
- temporary private object storage

The database stores identifiers, locations, hashes, properties, and state only.

## 16. Authentication and security

Cloud v0.1 is a private single-user or tightly controlled private application.

It is not anonymous and not publicly writable.

Required controls:

- HTTPS only.
- Authentication required for all application pages and APIs except health checks that expose no sensitive data.
- Strong session cookies.
- CSRF protection for state-changing browser requests.
- Rate limiting on login and submission endpoints.
- Private object storage.
- Expiring signed downloads.
- No permanent public URLs for media.
- Secrets stored outside source control.
- Parameterized database queries.
- HTML escaping of user and source metadata.
- CSV size and type restrictions.
- Job-level path isolation.
- Server-side output-path generation.
- No shell construction from unsanitized user input.
- Credential redaction in logs.
- Principle-of-least-privilege object-storage credentials.
- Restricted worker permissions.

### 16.1 Accounts

The first release does not need a general multi-user SaaS account system.

Acceptable first-release authentication approaches include:

- one private account
- identity-provider login restricted to an approved identity
- another simple private-access mechanism that meets the security requirements above

The final mechanism will be selected during deployment design.

## 17. Temporary file privacy

Temporary media should be treated as private content.

Requirements:

- Object-storage buckets are private.
- Object names should not expose unnecessary human-readable metadata.
- Signed URLs expire quickly.
- Deleted objects are not listed as available in the UI.
- Expired-job pages display metadata history without broken download promises.
- Application logs must not contain signed URLs beyond operationally necessary redacted identifiers.

## 18. Reliability and recovery

### 18.1 Worker interruption

If the worker stops unexpectedly:

- Active jobs become recoverable or `interrupted`.
- Partial downloads are evaluated before resumption.
- Verified source masters already present in scratch storage may be reused if integrity is confirmed.
- Temporary derivative files must not be trusted merely because they exist.
- A stage restarts only as far back as necessary.

### 18.2 Publication integrity

The system must not create a download link until:

1. The requested output exists.
2. FFprobe validation passes where applicable.
3. SHA-256 is calculated.
4. The final file is fully published to object storage.
5. The database records the publication atomically enough to avoid exposing a partial object as ready.

### 18.3 Expiration recovery

After files expire, the job history remains viewable.

If no permanent source exists, the UI must clearly state:

> Files expired. Reprocessing is required to create new downloads.

The system must not imply that expired media can be regenerated without reacquisition.

## 19. Efficiency and cost controls

Cloud v0.1 is optimized around minimizing retained bytes and unnecessary processing.

The system must:

1. Resolve metadata without downloading candidate media.
2. Acquire each approved source once per job unless retry is required.
3. Create all requested outputs from the local source master.
4. Avoid downloading video when audio-only is available.
5. Delete worker scratch files after successful publication.
6. Delete delivery objects after the retention window.
7. Use object-storage lifecycle deletion where available.
8. Avoid storing audio in PostgreSQL.
9. Avoid proxying large downloads through the application server when direct object delivery is available.
10. Process one job at a time initially.
11. Estimate long-form WAV size before conversion.
12. Avoid generating optional outputs the user did not request.
13. Keep metadata history small after media expiry.

## 20. Observability

The system must provide enough operational evidence to distinguish failures in:

- authentication
- form or CSV validation
- source resolution
- YouTube metadata access
- format selection
- PO-token generation
- media download
- HTTP 403 / access restriction
- source verification
- FFmpeg conversion
- output verification
- checksum generation
- packaging
- object-storage publication
- signed URL generation
- automatic deletion

Each job records:

- current stage
- stage timestamps
- concise user-facing status
- internal error class
- retry count
- worker identity
- tool versions
- final processing result
- file publication and expiry state

Sensitive token and authentication material must be redacted.

## 21. Functional requirements

### CFR-1 — Remote access

The application is reachable through a private authenticated HTTPS URL from standard modern browsers.

### CFR-2 — Manual entry

Accept artist, title, optional version, optional exact URL, and output profile.

### CFR-3 — CSV ingestion

Accept the v0.3 CSV schema, validate rows, preview results, and create one persistent job per accepted row.

### CFR-4 — Exact URL ingestion

Accept supported YouTube URL forms and bypass search.

### CFR-5 — Candidate resolution

Search and score up to five candidate sources and retain selection evidence.

### CFR-6 — Manual review

Allow an authenticated user to approve a candidate, provide another URL, or mark a job not found from any device.

### CFR-7 — Persistent queue

Persist queue state independently of any browser session.

### CFR-8 — Worker execution

A background worker processes jobs independently of the web request lifecycle.

### CFR-9 — Source quality

Apply the approved v0.3 source-master selection and verification policy.

### CFR-10 — Ableton output

Create and verify the approved 32-bit float Ableton intermediate.

### CFR-11 — Long-form output

Apply the existing segmentation policy before exceeding the configured safe WAV threshold.

### CFR-12 — Temporary publication

Publish verified completed assets to private temporary delivery storage.

### CFR-13 — Secure download

Provide authenticated access to expiring download URLs.

### CFR-14 — Native source download

Allow download of the verified source master while the delivery object remains active.

### CFR-15 — Archive package

Allow generation or publication of a ZIP package containing source, requested outputs, metadata, artwork, and checksums.

### CFR-16 — Automatic cleanup

Delete temporary delivery files after the configured retention period and remove scratch storage after it is no longer required.

### CFR-17 — Persistent history

Retain job metadata after media expiry without falsely representing expired media as archived.

### CFR-18 — Retry

Allow stage-appropriate retry after transient worker or network failures.

### CFR-19 — Access-failure classification

Classify YouTube access restrictions such as 403 and token failures distinctly from content-processing failures.

### CFR-20 — Worker portability

Do not couple the control plane to one permanent worker location or operating system.

### CFR-21 — Security

Protect the application, queue, temporary objects, credentials, and downloads from unauthenticated access.

### CFR-22 — Audit trail

Record requested metadata, resolution, approved source, tool versions, processing stages, quality status, hashes, publication, expiry, and deletion outcome.

## 22. Initial download profiles

| Profile | Temporary outputs | Intended use |
| --- | --- | --- |
| `ableton` | Ableton WAV + source metadata; source master downloadable | Default editing workflow |
| `source` | Native source master + preservation metadata | Smallest direct preservation download |
| `package` | Source + Ableton WAV + artwork + metadata + checksums ZIP | Complete local handoff |

An MP3 listening derivative may remain a later option, but it is not required to prove Cloud v0.1.

## 23. Deliberately excluded from Cloud v0.1

- Permanent cloud retention of all audio.
- Full cloud archive browsing by retained media asset.
- Public anonymous access.
- Public file sharing.
- General SaaS multi-tenancy.
- Billing or subscription management.
- Multiple simultaneous acquisition workers.
- Distributed conversion clusters.
- Spotify or Apple Music integration.
- Playlist ingestion.
- Automatic transcription.
- DJ-set track detection.
- Audio fingerprinting.
- Permanent mastered-output storage.
- Mobile native applications.
- Automatic synchronization to Ableton.
- CDN optimization beyond normal private object delivery.

## 24. Future storage modes

The architecture should support future per-job or account-level storage policies without changing the ingestion pipeline.

### Mode A — Download Only

```text
Acquire
  ↓
Process
  ↓
Download
  ↓
Delete all media
```

This is the Cloud v0.1 default.

### Mode B — Keep Source

```text
Keep permanently:
✓ Native source master
✓ source.info.json
✓ archive.json
✓ artwork
✓ checksums

Temporary:
○ Ableton WAV
○ MP3
○ other derivatives
```

This is the likely next preservation-oriented cloud mode because the native source is canonical and usually much smaller than PCM derivatives.

### Mode C — Full Cloud Archive

```text
Keep permanently:
✓ Native source
✓ metadata
✓ artwork
✓ Ableton intermediate
✓ derivatives
✓ mastered outputs when requested
```

This is explicitly deferred.

## 25. Acceptance criteria for Cloud v0.1

Cloud v0.1 is complete when all of the following are demonstrated:

1. The private application can be opened from a browser on a device other than the processing host.
2. Unauthenticated users cannot view the queue or submit jobs.
3. HTTPS is enforced.
4. Artist/title submission creates a persistent job.
5. Exact URL submission creates a persistent job and bypasses search.
6. CSV upload creates valid jobs and reports invalid rows independently.
7. A strong candidate can be auto-selected under the documented resolver policy.
8. An ambiguous candidate can be reviewed remotely.
9. Closing the submitting browser does not stop processing.
10. The worker acquires the approved source under the v0.3 quality policy.
11. The source master passes FFprobe and SHA-256 verification.
12. The default profile creates a verified 32-bit float PCM WAV.
13. Long-form jobs use the configured segmentation policy.
14. Completed files are copied to private delivery storage.
15. The user can download the Ableton WAV through an authenticated expiring link.
16. The user can download the native source master before expiry.
17. The user can download a complete archive package when requested.
18. No partial or unverified file is exposed as downloadable.
19. The UI displays a concrete expiration timestamp.
20. Delivery files are automatically deleted after the retention window.
21. Scratch files are cleaned after successful publication or documented failure cleanup.
22. The database retains job history after media deletion.
23. The UI clearly distinguishes `ready_to_download` from `files_expired`.
24. A transient processing failure can be retried without creating contradictory state.
25. A YouTube 403 or token/access failure is reported distinctly from an FFmpeg or verification failure.
26. Credentials and token material do not appear in Git, downloadable packages, manifests, or ordinary logs.
27. The web process and worker can be separated without redesigning the job model.
28. The worker can be replaced by another registered execution node in a future version without changing the browser workflow.

## 26. Test strategy

### 26.1 Deterministic tests

Retain applicable local v0.3 tests and add:

- authentication enforcement
- authorization of job access
- CSRF handling
- job submission persistence
- browser-independent worker processing
- PostgreSQL state transitions
- worker claim / release behavior
- idempotent publication
- object-storage key generation
- signed URL generation
- signed URL expiry
- delivery lifecycle timestamps
- early user deletion
- lifecycle cleanup reconciliation
- expired-file state behavior
- log redaction
- worker secret isolation
- recovery after worker interruption
- publication failure after successful conversion
- long-form object publication
- archive-package contents and checksums

### 26.2 Integration tests

- Submit from remote browser and process without persistent browser connection.
- Perform exact-URL source acquisition.
- Perform candidate-search acquisition.
- Complete Ableton conversion.
- Publish files to private object storage.
- Download files using signed links.
- Verify inaccessible object without authorization.
- Expire and delete delivery objects.
- Preserve database job history after object deletion.
- Restart worker during download and validate recovery behavior.
- Restart worker during conversion and validate recovery behavior.
- Simulate source-access 403 and confirm correct error classification.
- Validate PO-token provider integration without token leakage.

Network-dependent YouTube tests remain separate from deterministic tests because search results, formats, access requirements, and anti-abuse controls can change.

## 27. Deployment principles

Cloud v0.1 should favor a small number of boring, managed components over elaborate cloud-native infrastructure.

Preferred characteristics:

- one private web service
- one worker process
- one PostgreSQL database
- one private object-storage bucket
- one deployment pipeline
- one secrets store
- one domain
- one authentication path

Cloud v0.1 does not require Kubernetes, distributed microservices, Kafka, a service mesh, or multiple regions.

The logical separation between web, worker, database, and storage is required even when deployment starts on a minimal physical footprint.

## 28. Repository implications

The existing repository should evolve approximately toward:

```text
audio.archive/
  PROJECT_SPEC.md
  CLOUD_SPEC.md
  README.md
  handoff.md
  pyproject.toml
  config/
    base.toml
    profiles/
      ableton.toml
      source.toml
      package.toml
  src/
    audio_archive/
      app.py
      auth.py
      cli.py
      config.py
      db.py
      resolver.py
      queue.py
      worker.py
      pipeline.py
      publish.py
      cleanup.py
      storage.py
      verify.py
      web/
        templates/
        static/
  migrations/
  scripts/
  tests/
```

Exact module boundaries may change during implementation, but cloud-specific storage, publication, cleanup, authentication, and worker responsibilities must remain explicit.

## 29. Implementation sequence

No cloud implementation begins until this specification is approved.

After approval:

1. Lock the Cloud v0.1 job states and delivery lifecycle.
2. Select the initial cloud deployment provider and object-storage provider.
3. Select the private authentication method.
4. Define PostgreSQL schema and migrations.
5. Extract the existing ingestion pipeline cleanly from local filesystem / SQLite assumptions.
6. Implement worker job claiming and persistent execution.
7. Implement temporary scratch workspace management.
8. Implement private object-storage publication.
9. Implement signed-download delivery.
10. Implement automatic lifecycle cleanup.
11. Add cloud authentication and authorization.
12. Connect the existing manual-entry and exact-URL flows.
13. Connect candidate review.
14. Connect CSV ingestion.
15. Add cloud-specific logging and access-failure classification.
16. Add current PO-token provider support.
17. Run source-only acceptance test.
18. Run normal song Ableton acceptance test.
19. Run long DJ-set / segmented-output acceptance test.
20. Run remote-browser and browser-disconnect acceptance tests.
21. Run file-expiry and cleanup acceptance tests.
22. Document deployment, normal use, downloads, expiry, retry, and recovery.

## 30. Open implementation decisions

The following decisions are intentionally left open until deployment planning:

1. Initial hosting provider.
2. Object-storage provider.
3. PostgreSQL provider.
4. Authentication provider or private-login mechanism.
5. Exact queue implementation.
6. Exact worker runtime environment.
7. Whether initial web and worker processes share one host while remaining logically separate.
8. Whether direct cloud YouTube acquisition is sufficiently reliable for production use.
9. If not, which residential/local worker deployment becomes the acquisition fallback.
10. Exact signed-URL lifetime.
11. Whether 24-hour retention is user-configurable in v0.1 or fixed.
12. Whether the archive ZIP is built automatically or only on request.

These decisions may affect deployment configuration, but they must not alter the approved preservation or verification rules.

## 31. External operational constraints as of August 2026

YouTube extraction behavior is an external dependency and may change independently of Audio Archive.

Current yt-dlp guidance indicates:

- Some YouTube playback clients require Proof-of-Origin tokens for media access.
- yt-dlp recommends a PO Token provider plugin for current affected workflows.
- HTTP 403 failures may be associated with data-center IP addresses, VPN/proxy usage, outdated yt-dlp versions, IPv6 paths, or high-volume downloading.

Therefore the architecture intentionally separates the cloud control plane from the worker implementation and network location.

These external conditions are not Audio Archive quality rules. They are operational constraints that the worker layer must adapt to over time.

## 32. Changes from local specification v0.3

Cloud v0.1 introduces the following changes relative to the local design:

- Changes the primary access model from local Windows browser access to private authenticated browser access from anywhere.
- Replaces loopback-only networking with authenticated HTTPS.
- Replaces SQLite cloud state with PostgreSQL.
- Separates the web application from background worker execution.
- Replaces permanent local archive output with temporary cloud delivery by default.
- Introduces worker scratch storage and private temporary object storage.
- Adds expiring signed downloads.
- Adds a default 24-hour media retention window.
- Adds automatic lifecycle deletion.
- Adds `publishing`, `ready_to_download`, and `files_expired` lifecycle concepts.
- Keeps lightweight job history after media expiry.
- Adds private authentication, authorization, secrets management, and cloud security requirements.
- Adds explicit classification of cloud/network source-access failures.
- Adds worker portability so cloud acquisition can later move to another network or machine without changing the browser workflow.
- Defers permanent cloud source retention and full cloud archiving to later versions.
- Preserves the source-master, Ableton intermediate, resolver, verification, checksum, and quality-status principles of v0.3.

## 33. Cloud v0.1 product definition

The first cloud version of Audio Archive can be summarized as:

> **A private, browser-accessible audio ingestion and processing service that accepts a song request, YouTube URL, or CSV from anywhere; resolves and acquires the approved source under the Audio Archive quality policy; creates and verifies an Ableton-ready output; makes the verified files available through secure temporary downloads; and automatically deletes the cloud media after the retention window.**

Permanent cloud preservation remains a future storage mode rather than a requirement for the first cloud release.
