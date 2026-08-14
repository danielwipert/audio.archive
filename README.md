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

The application is intended only for media you own or are authorized to
download and archive.

