# Audio Archive — Implementation Decisions

This file records decisions that convert the approved product specification into
implementation constraints. It is not a session log.

## DEC-001 — Ableton compatibility target

- **Status:** Accepted
- **Date:** 2026-08-14

Ableton Live 12 is the primary editing target. Audio Archive will retain the
1.8 GiB safe WAV threshold and 60-minute segmentation default so long-form files
remain usable with Live 11 and earlier. No output property may be lowered
silently to remain beneath the threshold.

## DEC-002 — Portable default archive root

- **Status:** Accepted
- **Date:** 2026-08-14

The default archive root is `archive/` beneath the project directory. It is
resolved to an absolute path at runtime and can later be overridden by local
configuration. No drive letter or user-specific directory is embedded in code.

## DEC-003 — Persistent-state boundary

- **Status:** Accepted
- **Date:** 2026-08-14

SQLite schema version 1 owns queue and application state. Archive manifest
schema 1.2 owns the durable provenance of one completed item. The manifest and
checksums must keep an item intelligible if the SQLite database is lost.

## DEC-004 — Dependency updates

- **Status:** Accepted
- **Date:** 2026-08-14

The acquisition environment initially pins yt-dlp `2026.7.4`, the current stable
release when implementation began. Tool upgrades are explicit maintenance
events and never occur during an ordinary acquisition.

## DEC-005 — Full release before archive use

- **Status:** Accepted
- **Date:** 2026-08-17

The permanent archive will not begin with a partial alpha workflow. Normal use
starts only after all v0.3 first-release acceptance criteria pass on Windows.
Media used before that point is test material and must not be treated as the
beginning of the permanent collection.

## DEC-006 — YouTube JavaScript runtime

- **Status:** Accepted
- **Date:** 2026-08-17

Deno 2.3.0 or newer is required for acquisition. The project installs yt-dlp
with its matching default EJS components and supplies the controlled Deno path
explicitly. If that runtime is unavailable or too old, acquisition fails before
download rather than claiming that a restricted format set is the verified best
available source.

## DEC-007 — Portable Windows toolchain

- **Status:** Accepted
- **Date:** 2026-08-17

The Windows setup script installs missing Deno and FFmpeg packages through
Windows Package Manager, then copies Deno, FFmpeg, and FFprobe executables into
the project `tools/` directory. yt-dlp and its EJS components live in the
project virtual environment at exact application-compatible versions. Runtime
tool resolution prefers these project-controlled locations and does not embed a
drive letter or user-specific path. Updates occur only through the explicit
maintenance script.

## DEC-008 — Ableton conversion and segmentation

- **Status:** Accepted
- **Date:** 2026-08-17

Ableton intermediates are decoded from the verified local source master with
FFmpeg `pcm_f32le` and no audio filters, resampling, channel remapping,
normalization, or dither. Normal items produce one WAV. Items predicted to cross
the configured safe size use FFmpeg's segment muxer in one continuous decode;
each resulting WAV is FFprobe-verified and recorded with cumulative start and
end sample positions. A real generated-audio test requires the concatenated
segment PCM to be byte-identical to an unsegmented 32-bit float decode.

## DEC-009 — Listening derivative provenance

- **Status:** Accepted
- **Date:** 2026-08-17

Listening MP3s are optional compatibility derivatives made only from the
verified local source master. FFmpeg uses libmp3lame VBR quality scale 0, does
not normalize or deliberately change rate or channel layout, and embeds curated
title/artist tags plus a JPEG-encoded copy of the preserved source thumbnail.
FFprobe must confirm one MP3 audio stream, exactly one attached picture, the
source rate and mono/stereo layout, and the curated tags before publication.
Encoder settings, source hash, output hash, metadata, and artwork presence are
recorded in the durable manifest. A valid existing derivative is reused without
network access.

## DEC-010 — Sequential worker claims and recovery

- **Status:** Accepted
- **Date:** 2026-08-17

The first release uses one sequential queue worker. A SQLite `worker_claims`
lease prevents simultaneous workers from processing the same or different jobs
under a single-worker policy. The claim includes the local process ID so a new
launcher refuses to interrupt a live worker but can clear a stale crash claim.
Startup marks active jobs interrupted and requeues them to `ready` when a source
is pinned or `pending` when resolution is still required. Re-execution starts at
the last durable boundary: verified archive items and derivatives are validated
and reused, while yt-dlp may resume safe partial downloads in the job temporary
directory. One recorded failure releases its claim and does not block later jobs.

## DEC-011 — Candidate resolution and review boundary

- **Status:** Accepted
- **Date:** 2026-08-19

Artist/title jobs are resolved through a bounded metadata-only yt-dlp search using
the configured candidate limit. The existing deterministic scorer remains the
sole automatic-selection policy: the top candidate must meet the configured
minimum score and lead the runner-up by the configured margin, with no
unrequested disqualifying version term. Every ranked candidate, score, reason,
warning, and disqualification flag is persisted before the job leaves
`resolving`. Automatic selections are pinned before acquisition. Ambiguous jobs
enter `needs_review`; a user may approve a recorded candidate, supply a
replacement YouTube URL, or mark the request `not_found`. Review jobs release
the sequential-worker claim and do not block later queue work.

## DEC-012 — Toolchain version sources

- **Status:** Accepted
- **Date:** 2026-09-02
- **Amends:** DEC-004, DEC-006

The minimum supported Deno version is 2.4.3, raised from the 2.3.0 recorded by
DEC-006. The pinned BgUtils PO token script provider refuses to run below that
version, so an older runtime passes the readiness check and then silently loses
token generation, which degrades acquisition quality status rather than failing.
The minimum lives in one constant and is enforced by both `doctor` and
acquisition.

The expected yt-dlp version is read from the installed distribution rather than
repeated in application code. DEC-004 keeps its meaning: the pin is an explicit
maintenance decision recorded in `pyproject.toml`. Duplicating it a second time
allowed the readiness check to reject a correctly pinned toolchain after a
dependency update, which blocked the Windows launcher.

## DEC-013 — Chosen download formats

- **Status:** Accepted
- **Date:** 2026-09-03

A cloud job carries a set of requested outputs rather than one profile. The user
chooses any combination of the Ableton 32-bit float WAV, a 24-bit PCM WAV, an MP3
listening copy, and the archive package. The verified source master and its
sidecars are always published, so they are not part of the chosen set. The stored
profile remains as the coarse preset that summarizes a choice for provenance and
display; `requested_outputs` is what the worker acts on, and the durable manifest
records the exact set beside the preset.

The 24-bit PCM WAV is the alternative PROJECT_SPEC section 9.3 permits when the
user explicitly accepts integer quantization. It is recorded as a derivative
rather than as an intermediate: it is a compatibility copy for tools that dislike
32-bit float, it carries no more source information than the master, and the
Ableton intermediate remains the canonical editing target under DEC-008. Every
other DEC-008 rule applies unchanged to it — decoded only from the verified local
master, source sample rate and mono/stereo layout preserved, and no resampling,
filtering, normalization or dither. Both WAV variants come from one conversion
engine so a rule cannot hold for one and lapse for the other.

A 16-bit or CD-rate variant is deliberately not offered. It would require
resampling and dither, which section 8.3 forbids applying automatically, so it
would need its own decision rather than a checkbox.

All requested formats are created in one conversion stage from the single verified
source master. Asking for three files is one acquisition and one pass over that
master, never three trips through the queue.

## DEC-014 — Scratch reuse boundary

- **Status:** Accepted
- **Date:** 2026-09-03

A cloud job's scratch workspace is keyed on the job rather than on the worker
claim, and a failed attempt keeps it. The next attempt reuses what the previous
one proved instead of starting from an empty directory. This is the recovery
behaviour CLOUD_SPEC section 18.1 asks for, and it matters most now that a
rate-limited acquisition retries itself.

The durable boundary is the atomically published archive item, because it is the
only artifact that carries its own manifest and checksums and can therefore be
re-verified independently. An item is reused only when it re-verifies; one that
does not is deleted and re-acquired.

Job temporary files are always cleared. A partial download or a half-written
derivative cannot be distinguished from a complete one by inspection, so
resuming one would mean trusting a file for existing. yt-dlp resume across
attempts is given up deliberately: the expensive thing to repeat is a completed,
verified acquisition, and that is what reuse now preserves.

Retained workspaces are removed when the job reaches a terminal state, or after a
configurable retention window, so failed-job diagnostics do not accumulate
indefinitely on worker disk.
