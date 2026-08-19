# Audio Archive — Windows v0.3 Acceptance

Audio Archive remains pre-release until the automated Windows checks, an authorized live acquisition, and the manual launcher/Ableton checks have been completed on the target machine.

## 1. Run automated core acceptance

From PowerShell in the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows-acceptance.ps1 -RunSetup
```

This performs or verifies:

- the Windows toolchain and pinned project dependencies;
- the full Python test suite;
- integrity of existing permanent YouTube archive items;
- generation and verification of a normal Ableton 32-bit float WAV fixture;
- generation and verification of a segmented Ableton fixture through the same production conversion path.

The segmented fixture deliberately uses a tiny acceptance-only file-size threshold. This exercises the production segmentation code without creating a multi-gigabyte test file. It does not change the normal 1.8 GiB archive policy.

A Markdown report is written under `archive/acceptance/reports/`. That location is intentionally outside version control.

## 2. Run one authorized live complete acquisition

When you have a YouTube item you own or are authorized to archive, run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows-acceptance.ps1 `
  -AuthorizedUrl "https://www.youtube.com/watch?v=VIDEO_ID"
```

The acceptance runner creates a `complete` job for that exact URL, acquires the native source master, creates the Ableton and listening derivatives from the local master, and verifies the completed archive item.

The live URL is optional to the script because the repository cannot invent authorization. Release acceptance is still blocked until this test has actually been completed.

## 3. Complete the manual checks in the report

The generated report lists exact fixture paths. Complete its checkboxes on the target Windows machine, including:

- double-clicking `launch.cmd` and confirming the local application opens normally;
- confirming the browser stays on a loopback address;
- exercising artist/title input, CSV preview, queue behavior, and ambiguous-source review;
- opening the normal acceptance WAV in Ableton Live 12;
- opening the segmented WAV files in order and confirming the sequence is gapless;
- confirming neutral-import settings can be maintained;
- inspecting the authorized live `complete` item and its source master, derivatives, metadata, artwork, logs, manifest, and checksums.

## Release rule

Do not begin the permanent archive while the report says `BLOCKED` or while any applicable manual checkbox remains unverified. Pre-release downloads remain test material.
