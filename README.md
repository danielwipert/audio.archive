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

Audio Archive is not yet ready for normal archive use. Ableton conversion,
resolver search, the worker, the browser interface, and end-to-end acceptance
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

For an `archive` profile, a verified acquisition completes the job. Profiles
that require a derivative advance to `converting`; derivative execution is the
next implementation slice and is not yet available.

The application is intended only for media you own or are authorized to
download and archive.
