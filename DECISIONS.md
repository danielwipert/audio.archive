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

