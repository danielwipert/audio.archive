# Audio Archive — YouTube Ingestion Project Specification

**Version:** 0.3 (third draft)  
**Status:** Approved for implementation  
**Date:** 2026-08-14  
**Initial platform:** Windows  
**Primary editing environment:** Ableton Live  
**Repository:** `danielwipert/audio.archive`

## 1. Purpose

Build a fast, dependable, local application that can find and ingest authorized audio from YouTube with minimal user effort, preserve the best accessible native audio as a source master, and create an Ableton-ready working file without unnecessary quality loss.

The first release is a YouTube audio ingester with three equivalent input paths:

1. Artist and song title
2. Exact YouTube URL
3. CSV batch of artists and songs

The application is the first component of a broader Audio Archive system that may later support songs, DJ sets, lectures, interviews, and other long-form audio from additional sources.

The system is intended only for material the user owns or is authorized to download and archive.

## 2. Product principle

The daily workflow should feel simpler than the underlying preservation process.

The user should not need to understand yt-dlp arguments, codecs, containers, FFmpeg commands, checksums, or archive manifests. The application will expose a small number of meaningful choices while applying the approved quality and preservation policy automatically.

The first release will use:

- One local application
- One ingestion engine
- One persistent queue
- One archive database
- One single-screen interface
- One default Ableton workflow

Manual entries, CSV rows, and exact URLs must all create the same internal job type and pass through the same verification pipeline. No input method may implement a separate downloading or conversion path.

## 3. Target user experience

### 3.1 Launch

The user double-clicks `Audio Archive` on Windows. The launcher:

1. Confirms that required local tools are available.
2. Starts the application on the loopback interface only.
3. Opens the application in the default browser.
4. Restores any unfinished queue from the previous session.

The user should not need to open a terminal during ordinary use.

### 3.2 Single-item workflow

```text
Enter artist + song
        or
Paste an exact YouTube URL
        ↓
Resolve the intended source
        ↓
Auto-approve a strong match
or request one-click review
        ↓
Acquire native source master
        ↓
Create Ableton WAV
        ↓
Verify and open archive item
```

The single-item form contains:

- Artist
- Song title
- Optional version qualifier
- Optional exact YouTube URL
- Output profile, defaulting to `ableton`
- `Find and Rip` button

If an exact URL is supplied, the application bypasses search and uses that source. Artist, title, and version remain user-supplied catalog metadata and do not overwrite the source metadata returned by YouTube.

### 3.3 CSV workflow

The user can drag a CSV onto the application or choose it with a file picker. The application validates the file, previews the accepted and rejected rows, and creates queue jobs after the user selects `Start Queue`.

The minimum CSV is:

```csv
artist,title
Massive Attack,Teardrop
Portishead,Roads
Radiohead,Everything in Its Right Place
```

Optional columns are:

```csv
artist,title,version,url,profile
Massive Attack,Teardrop,album,,ableton
Portishead,Roads,live,https://www.youtube.com/watch?v=EXAMPLE,ableton
```

CSV rules:

- `artist` and `title` are required unless `url` is supplied.
- `version` may specify values such as `album`, `single`, `live`, `remix`, `instrumental`, or `remaster`.
- `url` bypasses artist/title resolution for that row.
- `profile` is optional and defaults to `ableton`.
- Headers are trimmed and compared case-insensitively.
- Blank rows are ignored.
- Invalid rows are rejected with a row number and useful message.
- A rejected row must not prevent valid rows from entering the queue.
- The application records the CSV filename, file checksum, import time, and source row number with each created job.

## 4. Primary output

For each successfully resolved source, the default workflow produces:

1. The best accessible native audio stream from YouTube, preserved without audio transcoding.
2. An Ableton-ready 32-bit float PCM WAV created locally from the source master.
3. A durable metadata record describing the requested song, resolved source, format selection, acquisition, conversion, and created files.
4. The source thumbnail and complete yt-dlp information record.
5. SHA-256 checksums and a clear success, warning, or failure result.

A source-only archive profile remains available for maximum speed and minimum storage use.

## 5. Lean system architecture

### 5.1 Architecture

```text
Windows launcher
      ↓
Local browser interface
      ↓
Python application
  ├── Input validation
  ├── Candidate resolver
  ├── Persistent queue
  ├── Ingestion engine
  ├── Derivative engine
  └── Verification engine
      ↓
yt-dlp + FFmpeg + FFprobe
      ↓
Archive files + SQLite state
```

### 5.2 Required components

The first release will use:

- Python application core
- FastAPI local server
- Server-rendered HTML
- One ordinary CSS file
- Minimal browser JavaScript for form submission, queue polling, and review actions
- SQLite for queue and archive state
- yt-dlp as a controlled subprocess
- FFmpeg and FFprobe as controlled subprocesses
- A Windows command or PowerShell launcher

### 5.3 Deliberately excluded infrastructure

The first release will not require:

- React, Vue, Angular, or another frontend framework
- Node or a frontend build pipeline
- Electron
- PostgreSQL or another database server
- Docker for ordinary Windows use
- Cloud hosting
- User accounts or authentication
- A YouTube Data API key
- A Spotify, Apple Music, or third-party metadata API
- Multiple download workers

The application binds only to `127.0.0.1` by default. It must not expose the interface to the local network or public internet.

## 6. Source resolution

### 6.1 Resolution objective

Artist and title identify an intended recording, not a unique YouTube asset. Resolution must therefore happen before quality selection.

The resolver must distinguish, when possible, among:

- Official album or single audio
- Music video audio
- Live performance
- Remix
- Remaster
- Instrumental
- Cover or tribute version
- Karaoke version
- Slowed, sped-up, pitched, or edited version
- Unofficial re-upload

The resolver's purpose is to select the intended recording. The ingestion engine's purpose is to select the best accessible audio stream from the resolved YouTube item. These are separate decisions and must be logged separately.

### 6.2 Candidate search

For an artist/title job without a URL, the application will:

1. Normalize the supplied artist, title, and version qualifier for comparison while preserving the original text.
2. Create a bounded YouTube search query.
3. Request up to five candidates through yt-dlp search.
4. Store candidate URL, video ID, title, channel, duration, thumbnail, and available source signals.
5. Score the candidates using deterministic rules.
6. Auto-select only when the best candidate passes the confidence policy.
7. Otherwise set the job to `needs_review`.

The initial search behavior will be equivalent to `ytsearch5:`. Search must be metadata-only; candidate media must not be downloaded until a source is selected.

### 6.3 Confidence scoring

The initial resolver score will use a 0–100 scale. It will consider:

- Exact or near-exact artist match
- Exact or near-exact title match
- Requested version match
- Credible official-source signals
- Unrequested version terms
- Cover, karaoke, tribute, reaction, tutorial, slowed, sped-up, or remix terms
- Candidate duration when an expected duration is available
- Separation between the highest and second-highest score

The scoring logic must be deterministic, fixture-tested, and visible in the review interface.

Initial auto-selection policy:

- Best candidate score must be at least 90.
- Best candidate must lead the second candidate by at least 15 points.
- No disqualifying unrequested-version term may be present.
- Any unresolved conflict sends the job to review.

These thresholds are initial implementation defaults and may be tuned through documented test evidence. They must not be changed silently.

### 6.4 Manual review

The review interface displays up to five candidate cards containing:

- Thumbnail
- YouTube title
- Channel
- Duration
- Match score
- Match reasons and warnings
- Open-on-YouTube link
- `Use This Source` action

Reviewing one job must not block unrelated queue jobs. The user can also paste a replacement URL or mark the row `not_found`.

### 6.5 Resolution safety rules

- Never select a candidate merely because it appears first in search results.
- Never silently substitute a live, remix, cover, karaoke, slowed, sped-up, or instrumental version unless requested.
- Never re-resolve to a different video after a candidate has been approved.
- Pin the approved YouTube video ID and URL before acquisition begins.
- If the pinned source becomes unavailable, fail the job and request user action.
- Preserve the user's requested metadata separately from YouTube source metadata.

## 7. Persistent job queue

### 7.1 Queue model

Every manual submission, URL, or accepted CSV row creates a persistent job. Jobs survive application restarts.

Required job states:

| State | Meaning |
| --- | --- |
| `pending` | Validated and waiting for processing |
| `resolving` | Searching and scoring candidate sources |
| `needs_review` | Candidate selection requires user input |
| `ready` | Source is pinned and acquisition can begin |
| `downloading` | Native source master is being acquired |
| `verifying_master` | Source master is being inspected and checksummed |
| `converting` | Ableton or listening derivative is being created |
| `verifying_output` | Created output is being inspected and checksummed |
| `completed` | Requested outputs passed verification |
| `completed_with_warnings` | Outputs succeeded but quality or source warnings exist |
| `failed` | Processing stopped with a recorded error |
| `interrupted` | A previously active job was stopped by application or system shutdown and awaits recovery |
| `skipped_duplicate` | Existing archive item satisfies the request |
| `not_found` | No source was approved |
| `cancelled` | Pending work was cancelled by the user |

### 7.2 Worker policy

The first release uses one sequential worker. It processes one active acquisition or conversion at a time.

This keeps state, throttling, partial-file recovery, logs, and user expectations simple. Jobs waiting for manual review do not block ready jobs below them.

The interface must provide:

- Start queue
- Pause after current job
- Resume queue
- Retry failed job
- Remove or cancel pending job
- Review ambiguous job
- Open completed item folder

### 7.3 Restart and recovery

On application launch:

- Jobs left in an active state are marked `interrupted` internally and evaluated for recovery.
- Valid completed assets are reused.
- yt-dlp partial downloads may resume when safe.
- Invalid temporary derivatives are removed or replaced only within the affected job's temporary directory.
- The user sees whether the job resumed, restarted a stage, or requires review.

The system must never infer completion solely from a filename's existence.

## 8. Quality principles

### 8.1 What “highest quality” means

For this project, **highest quality** means:

> The highest-ranked, preferably non-DRC audio stream that yt-dlp can access for the approved YouTube item and acquisition session, preserved without transcoding, resampling, normalization, or other signal processing.

This is the strongest claim the system can verify. It does not mean:

- The original studio master or lossless file that existed before upload.
- A lossless source, because YouTube delivery audio is normally already lossy.
- That converting the source to WAV or FLAC restores information removed by YouTube encoding.
- Guaranteed access to account-restricted, region-restricted, Premium-only, or token-restricted formats.

The archive must not label an acquisition `verified_best_available` when yt-dlp reports missing formats, failed JavaScript challenges, authentication restrictions, Proof-of-Origin token problems, an unavailable preferred format, or another warning that could have hidden a better stream.

### 8.2 Source master versus working file

```text
Approved YouTube item
  ↓
Best accessible native audio stream
  ↓
Source master — original Opus/AAC/etc.; no audio re-encode
  ↓ local decode only
Ableton intermediate — 32-bit float PCM WAV
  ↓ editing, mixing, mastering
Mastered renders — WAV/FLAC/MP3 as explicitly requested
```

- The **source master** is the canonical preservation asset and proof of what YouTube delivered.
- The **Ableton intermediate** is a lossless PCM decode made for reliable editing; it does not contain more source information than the native master.
- A **mastered render** is a new creative output and must never replace the source master or Ableton intermediate.

### 8.3 Prohibited automatic processing

Acquisition and Ableton-intermediate creation must not automatically apply:

- Loudness or peak normalization
- Replay-gain changes
- EQ, compression, limiting, clipping repair, or denoising
- Silence trimming, fades, crossfades, or channel remixing
- Tempo processing
- Resampling, unless an explicit project-rate conversion is requested
- Dither when writing the 32-bit float intermediate
- Lossy encoding of the source master

## 9. Asset specifications

### 9.1 Native source master

The source master must:

- Select yt-dlp's best accessible audio-only stream under the controlled selection policy in Section 13.
- Prefer a non-DRC stream over a DRC variant when the extractor exposes that distinction.
- Retain the original audio codec and encoded packets.
- Avoid audio-extraction options that invoke a lossy encoder.
- Avoid resampling and all audio filters.
- Keep descriptive metadata and artwork as sidecars.
- Receive a SHA-256 checksum after verification.

If no audio-only stream is accessible, the system may download a combined audio/video source and demux its audio using codec copy. It must not re-encode the audio, and the fallback must be recorded in the manifest.

### 9.2 Ableton intermediate

For normal-length items, the Ableton intermediate must be:

| Property | Required value |
| --- | --- |
| Container | WAV |
| Audio format | 32-bit float PCM (`pcm_f32le`) |
| Sample rate | Same as decoded source |
| Channels | Same as source, limited to mono or stereo |
| Normalization | Off |
| Resampling | None by default |
| Dither | None |
| Audio filters | None |
| Source | Local native master only |

The 32-bit float file preserves the decoder's output precision and avoids an unnecessary float-to-integer reduction before Ableton processing. It does not improve the fidelity of the lossy YouTube source.

### 9.3 Long-form intermediates

Uncompressed 32-bit float stereo WAV at 48 kHz is approximately 1.38 GB per hour. Ableton Live 11 and earlier impose a 2 GB audio-file limit, reached at roughly 93 minutes for that format.

The application must estimate output size before conversion:

1. Below the configured safe threshold, create one 32-bit float WAV.
2. Above the threshold, default to segmented 32-bit float WAV files with sample-accurate, gapless boundaries.
3. Record segment order, start/end sample, duration, and checksum.
4. Allow an explicit 24-bit PCM WAV or FLAC alternative when the user accepts integer quantization.
5. Never silently lower bit depth, sample rate, or channel count.

The initial safe threshold is 1.8 GiB. Segment duration defaults to 60 minutes.

### 9.4 Listening derivative

MP3 is a compatibility copy only. It must never appear between the YouTube source and Ableton intermediate.

When requested, the application will:

- Generate MP3 from the local native source master.
- Use FFmpeg/LAME's highest practical VBR quality setting.
- Embed curated metadata and cover art when supported.
- Record encoder settings and checksums.

## 10. Output profiles

| Profile | Output | Intended use |
| --- | --- | --- |
| `ableton` | Source master + metadata + artwork + 32-bit float WAV | Default; editing and mastering |
| `archive` | Source master + metadata + artwork | Fastest, smallest, preservation-only |
| `listen` | Source master + metadata + artwork + MP3 | Playback compatibility |
| `complete` | Source master + Ableton WAV + MP3 + all sidecars | All outputs immediately |

The GUI defaults to `ableton`. A missing intermediate or derivative must be creatable later from the local source master without reconnecting to YouTube.

## 11. Local GUI requirements

### 11.1 Layout

The first release will use one responsive page with four regions:

1. **Add audio** — artist, title, version, URL, and profile.
2. **Import CSV** — drag-and-drop, file picker, validation summary, and start action.
3. **Queue** — status, progress, source, warnings, and permitted actions.
4. **Needs review** — ambiguous candidate count and review entry point.

The interface should prioritize clarity over dashboard density. Advanced technical detail remains collapsed unless the user opens it.

### 11.2 Queue display

Each row displays:

- Requested artist and title
- Resolved YouTube title when different
- Current stage
- Overall status
- Progress percentage when available
- Quality status when complete
- Warning or concise failure reason
- Relevant action

### 11.3 Progress delivery

The browser will poll a small local status endpoint at a modest interval. WebSockets, push infrastructure, and a frontend state framework are not required for the first release.

### 11.4 Ableton handoff

Completed Ableton jobs display:

- Open WAV folder
- Copy WAV path
- Source sample rate
- Duration
- Whether output was segmented
- Neutral-import reminder

The reminder states:

- Match the Ableton Set sample rate when practical.
- Import the WAV, not the MP3 derivative.
- Disable Warp and clip fades when an unchanged transfer is desired.
- Leave Transpose and Detune unchanged unless intentional.
- Start with unity gain before mastering decisions.

## 12. Quality status

Every acquisition receives one of these statuses:

| Status | Meaning |
| --- | --- |
| `verified_best_available` | The preferred non-DRC audio-only stream was selected and no quality-limiting warnings were reported. |
| `best_available_with_warnings` | A usable stream was preserved, but conditions may have limited formats or only a DRC variant was accessible. |
| `fallback_source` | The preferred audio-only stream was unavailable and a lower-ranked or combined source was used. |
| `failed` | No valid source master was created or verification failed. |

Resolution confidence and acquisition quality are separate values. A manually approved source can receive `verified_best_available` for its accessible stream even though its resolver status was `manual_selection`.

Derivative creation receives its own status and must not change a successful acquisition status.

## 13. Controlled format selection

The ingestion engine must:

1. Ignore unrelated global yt-dlp configuration.
2. Request audio-only `bestaudio` first.
3. Allow a controlled combined-stream fallback only when audio-only is unavailable.
4. Preserve yt-dlp's extractor quality ranking rather than forcing a codec solely by nominal bitrate or extension.
5. Prefer non-DRC over DRC variants and record selected DRC status.
6. Record the available-format evidence needed to explain selection.
7. Avoid `--extract-audio`, `--audio-format`, or another option that would transcode the source master.

The first implementation uses behavior equivalent to:

```text
--ignore-config --no-playlist -f "bestaudio/best" --write-info-json --write-thumbnail
```

The production command will add controlled paths, progress output, logs, archive tracking, and security options. It must not embed metadata or artwork into the source master.

Nominal bitrate alone cannot prove that Opus is better or worse than AAC for every item. The default trusts yt-dlp's extractor ranking while retaining format evidence.

## 14. Efficiency requirements

The application will be optimized around avoiding unnecessary work rather than aggressive concurrency.

It must:

1. Resolve candidates without downloading media.
2. Download audio-only streams and avoid video when audio-only is available.
3. Preserve the native source master without transcoding.
4. Download each approved YouTube item only once per archive acquisition.
5. Generate all outputs from the local source master.
6. Detect previously archived YouTube IDs and reuse valid assets.
7. Estimate Ableton output size before conversion.
8. Stream local decoding to a temporary output rather than create extra intermediates.
9. Fetch source metadata once per acquisition whenever practical.
10. Atomically publish completed assets.
11. Allow controlled fragment concurrency within a single download when beneficial.
12. Process only one archive job at a time.
13. Keep dependency updates separate from normal ripping.
14. Keep the browser interface responsive while background work runs.

“Fast” does not mean bypassing verification. The workflow performs the minimum checks needed to confirm that files exist, are readable, have expected stream properties, and match their manifests.

## 15. Functional requirements

### FR-1 — Local launch

Start the application with one Windows launcher, bind to loopback only, open the browser, and restore the queue.

### FR-2 — Manual entry

Accept artist, title, optional version, optional URL, and profile from the single-item form.

### FR-3 — CSV ingestion

Accept and validate the defined CSV schema, preview results, and create one persistent job per accepted row.

### FR-4 — Exact URL ingestion

Accept standard YouTube, shortened YouTube, and YouTube Music URLs. Playlist ingestion requires an explicit future playlist mode.

### FR-5 — Candidate resolution

Search up to five candidates, compute deterministic scores, auto-select only above threshold, and preserve selection evidence.

### FR-6 — Manual review

Allow the user to approve a candidate, supply another URL, or mark the item not found.

### FR-7 — Persistent queue

Persist jobs, state transitions, timestamps, progress summaries, warnings, and retry information in SQLite.

### FR-8 — Best-audio selection

Record selected format ID, container, codec, bitrate, sample rate, channels, filesize, DRC status, audio-only/fallback status, and format warnings.

### FR-9 — Preservation metadata

Save the unmodified yt-dlp `.info.json` and a project-controlled `archive.json` manifest.

### FR-10 — Artwork

Save the best source thumbnail as a sidecar. Derivatives may receive embedded artwork; the source master remains independent.

### FR-11 — Ableton intermediate

Create a verified 32-bit float PCM WAV from the local source master, preserving source rate and channels by default and applying the long-form policy.

### FR-12 — Listening derivative

Create MP3 only when requested and only from the local source master.

### FR-13 — Integrity

Generate SHA-256 checksums for source masters, intermediates, derivatives, and preservation-critical metadata. Provide a verification command.

### FR-14 — Duplicate prevention

Deduplicate approved sources by extractor and source ID, such as `youtube:<video_id>`. Reuse valid existing assets and create only missing requested outputs.

### FR-15 — Error handling

Record stage-specific errors, show a concise user message, retain diagnostic logs, and never report unconditional success.

### FR-16 — Dependency management

Resolve pinned tools relative to the project or configured tools directory. Setup installs or locates yt-dlp, Deno, FFmpeg, and FFprobe without hard-coded drive paths.

### FR-17 — Optional authenticated access

Allow an explicitly enabled local authentication profile for entitled content. Cookies or tokens must never enter the repository, manifest, CSV record, log, or visible command history.

### FR-18 — Warning classification

Capture quality-affecting yt-dlp warnings, including JavaScript-runtime, challenge component, authentication, format, and token failures.

### FR-19 — Output regeneration

Regenerate non-canonical outputs from a verified local source master without a second network acquisition.

### FR-20 — Audit trail

Record job origin, requested metadata, resolution decision, approved source, state transitions, tool versions, commands, checksums, and final result.

## 16. Data and deduplication model

### 16.1 SQLite role

SQLite is the application state store for:

- Queue jobs
- Job events and stage transitions
- CSV import provenance
- Candidate sets and resolver scores
- Approved source IDs
- Archive item locations
- Output availability
- Retry and failure summaries

SQLite is not the sole preservation record. Each completed archive item retains its own manifest and checksums so it remains intelligible if the application database is lost.

### 16.2 Duplicate rules

- Exact duplicate rows within one import are collapsed before processing and reported.
- After resolution, YouTube video ID is the canonical acquisition deduplication key.
- If a verified source master already exists, acquisition is skipped.
- If the requested profile requires a missing output, create it from the existing source master.
- Distinct YouTube IDs are not merged merely because artist and title match.
- The user may explicitly request a new acquisition version in a future workflow.

### 16.3 Sources of truth

- SQLite is the source of truth for active application state and queue history.
- `archive.json` is the source of truth for one archived item's provenance and assets.
- `SHA256SUMS` is the source of truth for recorded file integrity.
- yt-dlp `.info.json` is the unmodified source metadata record.

## 17. Archive structure

```text
archive/
  app-data/
    archive.db
  items/
    youtube/
      <video_id>/
        master/
          <video_id>.<native_ext>
        intermediates/
          ableton/
            <video_id>.wav
            segments/
              <video_id>.part-001.wav
        derivatives/
          listen/
            <video_id>.mp3
          mastered/
        artwork/
          source-thumbnail.<ext>
        metadata/
          source.info.json
          archive.json
        checksums/
          SHA256SUMS
        logs/
          ingest.log
          convert.log
  temp/
    <job_id>/
```

Human-readable titles belong in metadata and interfaces. Stable source IDs determine physical storage identity. Temporary directories may be cleaned after verified success and must never be mistaken for archived assets.

## 18. Archive manifest

The project-controlled `archive.json` includes at least:

```json
{
  "schema_version": "1.2",
  "archive_id": "youtube:VIDEO_ID",
  "content_type": "song",
  "request": {
    "artist": "Requested artist",
    "title": "Requested title",
    "version": "album",
    "origin": "csv",
    "import_file_sha256": "checksum",
    "import_row": 2
  },
  "resolution": {
    "method": "automatic",
    "resolver_version": "1.0",
    "selected_score": 96,
    "runner_up_score": 72,
    "selected_video_id": "VIDEO_ID",
    "reviewed_by_user": false
  },
  "source": {
    "platform": "youtube",
    "id": "VIDEO_ID",
    "url": "https://www.youtube.com/watch?v=VIDEO_ID",
    "title": "YouTube source title",
    "creator": "Source channel"
  },
  "acquisition": {
    "acquired_at_utc": "ISO-8601 timestamp",
    "quality_status": "verified_best_available",
    "yt_dlp_version": "version",
    "ffmpeg_version": "version",
    "deno_version": "version",
    "quality_warnings": []
  },
  "source_master": {
    "path": "master/VIDEO_ID.webm",
    "format_id": "format identifier",
    "container": "webm",
    "audio_codec": "opus",
    "is_drc": false,
    "reported_bitrate_kbps": null,
    "sample_rate_hz": 48000,
    "channels": 2,
    "sha256": "checksum"
  },
  "intermediates": [
    {
      "role": "ableton",
      "path": "intermediates/ableton/VIDEO_ID.wav",
      "audio_format": "pcm_f32le",
      "sample_rate_hz": 48000,
      "channels": 2,
      "normalization": false,
      "resampled": false,
      "dithered": false,
      "source_sha256": "source checksum",
      "sha256": "checksum"
    }
  ],
  "derivatives": []
}
```

The precise schema will be finalized during implementation, but these concepts and asset roles are required.

## 19. Proposed repository structure

```text
audio.archive/
  PROJECT_SPEC.md
  README.md
  LICENSE
  pyproject.toml
  launch.cmd
  config/
    base.toml
    profiles/
      ableton.toml
      archive.toml
      listen.toml
      complete.toml
  src/
    audio_archive/
      app.py
      cli.py
      config.py
      db.py
      resolver.py
      worker.py
      pipeline.py
      verify.py
      web/
        templates/
          index.html
        static/
          app.css
          app.js
  scripts/
    setup.ps1
    update-tools.ps1
    verify-archive.ps1
  tests/
    fixtures/
  tools/
    .gitkeep
  archive/
    .gitignore
```

The Python core owns orchestration, state, and policy. yt-dlp handles search metadata and acquisition. FFmpeg and FFprobe handle inspection, codec-copy demuxing, PCM decoding, segmentation, and derivative creation. The command-line interface and browser interface both call the same pipeline.

## 20. Reliability, safety, and privacy

- Never overwrite a valid source master silently.
- Never delete a source master after output creation.
- Never treat partial media as complete.
- Never record success before FFprobe and checksum verification.
- Never silently substitute MP3 for a source master or Ableton intermediate.
- Never silently replace an approved YouTube source with another search result.
- Never expose the GUI beyond loopback by default.
- Never place credentials in the repository, database, manifest, CSV provenance, logs, or command history.
- Sanitize filenames and restrict output to the configured archive root.
- Use parameterized SQLite queries.
- Treat imported CSV fields and source metadata as untrusted text in HTML.
- Limit uploaded input to CSV and enforce a configurable file-size ceiling.
- Log actual command outcomes and tool versions.
- Preserve original source metadata when curated metadata is added.
- Publish completed assets atomically where the filesystem permits.

## 21. Acceptance criteria for the first release

The first release is complete when all of the following are demonstrated:

1. Double-clicking the Windows launcher opens the local application without terminal use.
2. The server binds to loopback only.
3. A user can enter artist and title and create a job.
4. A strong fixture match is auto-selected under the documented threshold.
5. An ambiguous match enters `needs_review` and can be approved from candidate cards.
6. An exact URL bypasses search and pins the correct source.
7. A valid CSV creates one job per accepted row and reports rejected rows clearly.
8. One invalid CSV row does not block valid rows.
9. Queue state survives application restart.
10. A waiting review job does not block unrelated ready jobs.
11. The system selects the best accessible, preferably non-DRC audio-only stream.
12. FFprobe confirms that the source master contains audio and no unnecessary video stream.
13. The source master's audio packets are not transcoded.
14. Source info JSON, archive manifest, thumbnail, logs, and checksums are written.
15. The default profile creates an Ableton-compatible 32-bit float PCM WAV.
16. The WAV preserves source sample rate and channels and receives no normalization, resampling, or dither.
17. Long-form size prediction creates ordered, gapless segments above the threshold.
18. Repeating an existing YouTube ID reuses the source master and creates only missing outputs.
19. MP3 can be created without another YouTube download.
20. A failed stage reports failure rather than `Done` and can be retried.
21. Moving the project to another Windows directory requires no hard-coded path edits.
22. The archive verification command validates recorded checksums.
23. Quality-limiting warnings change acquisition status and appear in the GUI.
24. A completed job exposes the correct Ableton file or segment folder.
25. Tests cover a song, a DJ set longer than 93 minutes, and a lecture-length item.

## 22. Test strategy

### 22.1 Deterministic tests

- CSV header, encoding, quoting, blank-row, and validation behavior
- Duplicate CSV rows
- Resolver normalization and score calculation
- Exact title and artist matching
- Requested and unrequested version handling
- Cover, karaoke, live, remix, slowed, and sped-up penalties
- Auto-selection threshold and runner-up margin
- Manual candidate approval
- Job-state transition validity
- Restart and interruption recovery
- SQLite migrations and parameterized writes
- HTML escaping of imported and source text
- Format-selection parsing and warning classification
- Manifest creation and schema validation
- FFprobe stream verification
- Source-master checksum verification
- 32-bit float WAV properties
- Source-rate and channel preservation
- Long-form size prediction
- Segment continuity and total sample count
- Archive-ID duplicate detection
- Paths containing spaces and non-ASCII characters
- Protection against global yt-dlp configuration leakage

### 22.2 Integration tests

- Local server starts and binds only to loopback.
- Browser form creates a persistent job.
- CSV import creates the expected jobs.
- Queue progresses without blocking the HTTP interface.
- yt-dlp search returns candidate metadata without downloading media.
- Approved source proceeds through acquisition and verification.
- Restart resumes or safely restarts interrupted work.
- Ableton opens the generated WAV in the supported target version.

Network-dependent tests must remain separate from deterministic fixtures because YouTube search results, formats, and access conditions can change.

## 23. Out of scope for the first release

- Remote or multi-user access
- Cloud-hosted ripping
- Mobile application
- Electron or installed native desktop shell
- YouTube playlist ingestion
- Spotify or Apple Music catalog matching
- Third-party canonical music metadata
- Automatic discography building
- Automatic mastering decisions or presets
- Ableton Set generation
- Automated transcription or speaker diarization
- Audio fingerprinting
- Automatic chapter or track-split detection
- Distributed or high-volume parallel downloading
- A full public-facing searchable catalog

## 24. Future extension points

- Playlist and album imports
- Paste-many text input
- Folder watching for CSV imports
- Spotify or Apple Music playlist resolution
- MusicBrainz or Discogs metadata enrichment
- Audio fingerprinting and duplicate detection
- DJ-set cue sheets, chapters, and track lists
- Lecture transcripts, subtitles, and speaker labels
- Multiple acquisition versions of the same source
- Curated metadata workflows
- Additional source platforms
- Searchable archive catalog
- Storage migration and integrity audits
- Ableton Set templates and handoff files
- Optional packaged desktop shell

## 25. Implementation sequence

No implementation begins until this specification is approved.

After approval, work proceeds in this order:

1. Confirm target Ableton Live version and archive root.
2. Lock SQLite tables, job states, and manifest schema.
3. Build the portable tool setup and configuration loader.
4. Implement the pipeline as a command-line callable core.
5. Implement native-master acquisition and verification.
6. Implement Ableton conversion and long-form segmentation.
7. Implement SQLite queue, state transitions, and restart recovery.
8. Implement exact-URL jobs.
9. Implement deterministic candidate resolver and fixture tests.
10. Implement CSV import and validation.
11. Implement the single-screen local GUI.
12. Connect GUI actions to the common job pipeline.
13. Add warning classification, retry behavior, and logs.
14. Run song, long DJ-set, and lecture acceptance tests.
15. Document setup, normal use, Ableton handoff, recovery, and regeneration.

## 26. Technical references

- [yt-dlp README, search support, and format selection](https://github.com/yt-dlp/yt-dlp)
- [yt-dlp configuration](https://github.com/yt-dlp/yt-dlp#configuration)
- [yt-dlp post-processing options](https://github.com/yt-dlp/yt-dlp#post-processing-options)
- [yt-dlp JavaScript runtime announcement](https://github.com/yt-dlp/yt-dlp/issues/15012)
- [yt-dlp Proof-of-Origin token guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
- [FFmpeg streamcopy documentation](https://ffmpeg.org/ffmpeg.html#Stream-copy)
- [Python standard library](https://docs.python.org/3/library/)
- [Python CSV module](https://docs.python.org/3/library/csv.html)
- [Python SQLite module](https://docs.python.org/3/library/sqlite3.html)
- [FastAPI templates](https://fastapi.tiangolo.com/advanced/templates/)
- [FastAPI static files](https://fastapi.tiangolo.com/tutorial/static-files/)
- [Ableton supported audio file formats](https://help.ableton.com/hc/en-us/articles/211427589-Supported-Audio-File-Formats)
- [Ableton Live 12 Audio Fact Sheet](https://www.ableton.com/en/live-manual/12/audio-fact-sheet/)
- [Ableton 2 GB guidance for Live 11 and earlier](https://help.ableton.com/hc/en-us/articles/209768905-File-import-export-or-recording-fails-due-to-2-GB-file-size-limit)
- [Ableton MP3 import behavior](https://help.ableton.com/hc/en-us/articles/115000331090-Using-MP3-files)

## 27. Changes from version 0.2

- Added a single-screen local browser GUI to the first-release scope.
- Added artist/title input and exact-URL bypass.
- Added CSV batch ingestion with a minimal schema and validation preview.
- Added deterministic candidate resolution, confidence scoring, and manual review.
- Added a persistent SQLite queue with restart recovery and retry behavior.
- Changed orchestration from PowerShell-centered to a shared Python application core.
- Defined a lean FastAPI, server-rendered HTML, CSS, and minimal-JavaScript architecture.
- Added loopback-only networking and local-input security requirements.
- Added request metadata, CSV provenance, and resolution evidence to the manifest.
- Added queue-state, resolver, GUI, CSV, and restart acceptance tests.
- Preserved the native source-master and Ableton-quality model from version 0.2.
