# Audio Archive

Audio Archive is a local, preservation-first application for ingesting authorized
audio from YouTube. It preserves the best accessible native audio stream without
transcoding, then creates verified derivatives such as a 32-bit float WAV for
Ableton Live.

The repository is in active development. The current foundation includes:

- portable TOML configuration;
- a persistent SQLite job queue and audited state transitions;
- exact YouTube URL validation;
- CSV validation, provenance, and within-import deduplication;
- deterministic candidate scoring and automatic-selection policy;
- output-size and long-form segmentation planning; and
- a command-line interface shared by the future browser UI.

The native-master acquisition slice is also implemented and deterministic-test
verified. It uses a controlled yt-dlp command, requires Deno for full YouTube
format access, preserves the source info record and thumbnail, verifies media
with FFprobe, demuxes combined fallbacks with codec copy, writes SHA-256
integrity records, and atomically publishes a complete archive item.

The Ableton derivative slice decodes only from that verified local master. It
creates 32-bit float PCM WAV without normalization, resampling, channel remixing,
filters, or dither; preserves the source rate and mono/stereo layout; segments
long-form audio beneath the configured safe size; records exact contiguous sample
boundaries; and updates the manifest and checksums transactionally.

The listening derivative slice also reads only the verified local master. It
encodes a highest-quality VBR MP3 with libmp3lame, embeds curated title and
artist tags plus the preserved source thumbnail, verifies all streams and tags
with FFprobe, and records the encoder settings and checksums transactionally.
Existing valid listening derivatives are reused without contacting YouTube.

The sequential background worker claims one runnable job in SQLite, executes
acquisition and every requested derivative through the shared pipeline, then
releases the claim. A failed job is recorded without blocking later work. On a
safe restart, stale claims are cleared, active jobs become interrupted, and
they are requeued from the last durable boundary; a live worker is never
interrupted by a second launcher.

Audio Archive is not yet ready for normal archive use. Resolver search, the
browser interface, Windows/Ableton acceptance, and live authorized end-to-end
tests remain incomplete.

## Windows setup

Python 3.11 or newer and Windows Package Manager (`winget`) are required. Run
the one-time setup from PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Setup creates the project virtual environment, installs the pinned yt-dlp and
matching EJS components, locates or installs Deno, FFmpeg, and FFprobe, copies
the external executables into `tools/`, initializes the archive, and runs the
complete readiness check. It does not update tools during ordinary ingestion.

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

Initialize the local database and inspect the CLI:

```powershell
audio-archive init
audio-archive --help
```

The development-only acquisition command accepts the ID of a `ready` job:

```powershell
audio-archive acquire 1
```

For an `ableton` or `complete` profile, create or reuse the verified Ableton
intermediate after acquisition:

```powershell
audio-archive convert-ableton 1
```

For a `listen` or `complete` profile, create or reuse the verified listening MP3:

```powershell
audio-archive convert-listening 1
```

The `ableton` and `listen` profiles complete after their requested output is
verified. The `complete` profile completes only after both outputs exist and
pass verification, regardless of which conversion command runs first.

Recover unfinished work and process every runnable exact-URL job sequentially:

```powershell
audio-archive run-queue
```

Use `run-queue --once` to process at most one job. Failed or interrupted work
can be explicitly requeued with `audio-archive retry JOB_ID`; waiting work can
be cancelled with `audio-archive cancel JOB_ID`. Search-based pending jobs stay
queued until resolver search and manual review are connected.

Verify one item by YouTube ID, or verify the complete archive:

```powershell
audio-archive verify VIDEO_ID
powershell -ExecutionPolicy Bypass -File scripts\verify-archive.ps1 -All
```

Verification cross-checks `SHA256SUMS`, the archive manifest, and every canonical,
intermediate, and derivative asset recorded by the manifest.

The application is intended only for media you own or are authorized to
download and archive.
