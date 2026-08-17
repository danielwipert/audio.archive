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
