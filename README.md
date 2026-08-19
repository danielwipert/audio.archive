# Audio Archive

Audio Archive is a local, preservation-first application for ingesting authorized
audio from YouTube. It preserves the best accessible native audio stream without
transcoding, then creates verified derivatives such as a 32-bit float WAV for
Ableton Live.

The current application includes:

- portable TOML configuration and a loopback-only local server;
- a persistent SQLite queue with audited state transitions and crash recovery;
- exact YouTube URL ingestion;
- CSV preview, validation, provenance, and within-import deduplication;
- bounded yt-dlp candidate search plus deterministic scoring and manual review;
- native source-master acquisition and FFprobe verification;
- Ableton 32-bit float WAV creation with long-form segmentation;
- optional listening MP3 generation from the verified local source master;
- SHA-256 manifests and archive verification; and
- a single-screen browser interface over the shared queue and pipeline.

Artist/title jobs use metadata-only yt-dlp search with the configured candidate
limit. Ranked candidates, scores, reasons, warnings, and disqualification flags
are persisted before resolution finishes. Strong matches are pinned
automatically under the configured score and margin policy. Ambiguous matches
enter `needs_review`, where a recorded candidate can be approved, a replacement
YouTube URL can be supplied, or the request can be marked `not_found`.

The native-master acquisition pipeline uses a controlled yt-dlp command, requires
Deno for full YouTube format access, preserves the source info record and
thumbnail, verifies media with FFprobe, demuxes combined fallbacks with codec
copy, writes SHA-256 integrity records, and atomically publishes a complete
archive item.

Ableton intermediates decode only from the verified local master. They use
32-bit float PCM WAV without normalization, resampling, channel remixing,
filters, or dither; preserve the source rate and mono/stereo layout; segment
long-form audio beneath the configured safe size; record exact contiguous sample
boundaries; and update manifests and checksums transactionally.

Listening MP3s also read only the verified local master. They use libmp3lame VBR
quality scale 0, embed curated title and artist tags plus the preserved source
thumbnail, and receive FFprobe verification before transactional publication.
Existing valid derivatives are reused without contacting YouTube.

The sequential background worker handles resolution, acquisition, and requested
derivatives through the same persistent job model used by the browser and CLI.
Automatic matches can continue directly into acquisition. Review jobs release
the worker so later work continues. Startup recovery requeues interrupted jobs
from the last durable boundary while refusing to disturb a live worker.

The browser interface uses server-rendered HTML, one CSS file, and minimal
JavaScript. It supports manual artist/title or exact-URL jobs, CSV preview and
import, live queue status, pause/resume, retry/cancel, candidate review,
replacement URLs, not-found decisions, completed-item folders, and Ableton path
handoff. Source and imported text are rendered as text rather than HTML.

Audio Archive is still pre-release. Windows/Ableton acceptance and one authorized
live end-to-end YouTube acquisition must pass before the permanent archive begins.

## Windows setup

Python 3.11 or newer and Windows Package Manager (`winget`) are required. Run the
one-time setup from PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Setup creates the project virtual environment, installs the pinned yt-dlp and
matching EJS components, locates or installs Deno, FFmpeg, and FFprobe, copies
the external executables into `tools/`, initializes the archive, and runs the
complete readiness check. It does not update tools during ordinary ingestion.

After setup, normal use is intended to start with a double-click on:

```text
launch.cmd
```

The launcher runs the readiness check, starts Audio Archive on the configured
loopback host, and opens the local application in the default browser. The
`serve` command refuses non-loopback hosts.

You can repeat the non-mutating readiness check at any time:

```powershell
.venv\Scripts\python.exe -m audio_archive doctor
```

External tool updates are an explicit maintenance action:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\update-tools.ps1
```

## Development setup

Python 3.11 or newer is required. On Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

Initialize the local database, inspect the CLI, or launch the browser application:

```powershell
audio-archive init
audio-archive --help
audio-archive serve
```

A pending artist/title job can be resolved directly during development:

```powershell
audio-archive resolve 1
audio-archive candidates 1
```

If the job requires review, approve a recorded video ID, supply a replacement
URL, or mark it not found:

```powershell
audio-archive approve 1 VIDEO_ID
audio-archive replace-source 1 https://www.youtube.com/watch?v=VIDEO_ID
audio-archive not-found 1
```

The development-only acquisition and derivative commands remain available:

```powershell
audio-archive acquire 1
audio-archive convert-ableton 1
audio-archive convert-listening 1
```

The `ableton` and `listen` profiles complete after their requested output is
verified. The `complete` profile completes only after both outputs exist and
pass verification, regardless of conversion order.

Recover unfinished work and process every runnable job sequentially from the CLI:

```powershell
audio-archive run-queue
```

Use `run-queue --once` to process at most one job. Pending artist/title jobs are
resolved automatically by the same worker. Jobs that enter `needs_review` are
left for a user decision while later runnable jobs continue. Failed or
interrupted work can be explicitly requeued with `audio-archive retry JOB_ID`;
waiting work can be cancelled with `audio-archive cancel JOB_ID`.

Verify one item by YouTube ID, or verify the complete archive:

```powershell
audio-archive verify VIDEO_ID
powershell -ExecutionPolicy Bypass -File scripts\verify-archive.ps1 -All
```

Verification cross-checks `SHA256SUMS`, the archive manifest, and every canonical,
intermediate, and derivative asset recorded by the manifest.

The application is intended only for media you own or are authorized to
download and archive.
