param(
    [switch]$RunSetup,
    [string]$AuthorizedUrl,
    [string]$ReportPath
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")

if ($RunSetup) {
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "setup.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Audio Archive setup failed before acceptance could begin."
    }
}

if (-not (Test-Path $Python)) {
    throw "Audio Archive is not set up. Run scripts\setup.ps1 or rerun with -RunSetup."
}

if (-not $ReportPath) {
    $ReportPath = Join-Path $ProjectRoot "archive\acceptance\reports\windows-v0.3-$Timestamp.md"
}
$ReportPath = [IO.Path]::GetFullPath($ReportPath)
New-Item -ItemType Directory -Path (Split-Path -Parent $ReportPath) -Force | Out-Null

function Invoke-PythonStep {
    param(
        [string]$Name,
        [string[]]$Arguments
    )

    Write-Host "==> $Name"
    try {
        $Output = (& $Python @Arguments 2>&1 | Out-String).TrimEnd()
        $ExitCode = $LASTEXITCODE
        if ($null -eq $ExitCode) {
            $ExitCode = 0
        }
        if ($Output) {
            Write-Host $Output
        }
        return [PSCustomObject]@{
            Name = $Name
            Passed = ($ExitCode -eq 0)
            ExitCode = $ExitCode
            Output = $Output
        }
    } catch {
        return [PSCustomObject]@{
            Name = $Name
            Passed = $false
            ExitCode = 1
            Output = $_.Exception.Message
        }
    }
}

function Failure-Step([string]$Name, [string]$Message) {
    return [PSCustomObject]@{
        Name = $Name
        Passed = $false
        ExitCode = 1
        Output = $Message
    }
}

function Status-Text([bool]$Passed) {
    if ($Passed) { return "PASS" }
    return "FAIL"
}

$Doctor = Invoke-PythonStep "Toolchain readiness" @("-m", "audio_archive", "doctor")
$Tests = Invoke-PythonStep "Full deterministic and integration test suite" @("-m", "pytest")
$ArchiveVerify = Invoke-PythonStep "Existing archive integrity" @(
    "-m", "audio_archive", "verify", "--all"
)
$Fixtures = Invoke-PythonStep "Ableton acceptance fixture generation" @(
    "-m", "audio_archive.acceptance", "--json"
)

$FixtureData = $null
if ($Fixtures.Passed) {
    try {
        $FixtureData = $Fixtures.Output | ConvertFrom-Json
    } catch {
        $Fixtures.Passed = $false
        $Fixtures.ExitCode = 1
        $Fixtures.Output = "Fixture generator returned invalid JSON: $($_.Exception.Message)"
    }
}

$LiveStatus = "PENDING"
$LiveDetail = "No authorized YouTube URL supplied."
$LiveJobId = $null
$LiveVideoId = $null
$LiveSteps = @()

if ($AuthorizedUrl) {
    $Add = Invoke-PythonStep "Create authorized complete-profile job" @(
        "-m", "audio_archive", "add", "--url", $AuthorizedUrl, "--profile", "complete"
    )
    $LiveSteps += $Add

    if ($Add.Passed -and $Add.Output -match "Created job\s+(\d+)") {
        $LiveJobId = [int]$Matches[1]
        $Acquire = Invoke-PythonStep "Acquire authorized native source" @(
            "-m", "audio_archive", "acquire", "$LiveJobId"
        )
        $LiveSteps += $Acquire

        if ($Acquire.Passed) {
            $Ableton = Invoke-PythonStep "Create authorized Ableton derivative" @(
                "-m", "audio_archive", "convert-ableton", "$LiveJobId"
            )
            $LiveSteps += $Ableton

            if ($Ableton.Passed) {
                $Listening = Invoke-PythonStep "Create authorized listening derivative" @(
                    "-m", "audio_archive", "convert-listening", "$LiveJobId"
                )
                $LiveSteps += $Listening
            }
        }

        $Id = Invoke-PythonStep "Resolve authorized source ID" @(
            "-c",
            "from audio_archive.urls import parse_youtube_url; import sys; print(parse_youtube_url(sys.argv[1]).video_id)",
            $AuthorizedUrl
        )
        $LiveSteps += $Id
        if ($Id.Passed) {
            $LiveVideoId = $Id.Output.Trim()
        }

        $PriorFailures = ($LiveSteps | Where-Object { -not $_.Passed }).Count
        if ($PriorFailures -eq 0 -and $LiveVideoId) {
            $VerifyLive = Invoke-PythonStep "Verify authorized complete archive item" @(
                "-m", "audio_archive", "verify", $LiveVideoId
            )
            $LiveSteps += $VerifyLive
        }
    } elseif ($Add.Passed) {
        $LiveSteps += Failure-Step "Parse created job" "The add command did not return a job ID."
    }

    if ($LiveSteps.Count -gt 0 -and ($LiveSteps | Where-Object { -not $_.Passed }).Count -eq 0) {
        $LiveStatus = "PASS"
        $LiveDetail = "Authorized complete-profile job $LiveJobId verified as youtube:$LiveVideoId."
    } else {
        $LiveStatus = "FAIL"
        $Failures = $LiveSteps | Where-Object { -not $_.Passed }
        $LiveDetail = ($Failures | ForEach-Object { "$($_.Name): $($_.Output)" }) -join " | "
    }
}

$CorePass = $Doctor.Passed -and $Tests.Passed -and $ArchiveVerify.Passed -and $Fixtures.Passed
$AutomatedStatus = if ($CorePass) { "PASS" } else { "FAIL" }
$ReleaseStatus = if (-not $CorePass -or $LiveStatus -eq "FAIL") {
    "BLOCKED — automated acceptance failed"
} elseif ($LiveStatus -ne "PASS") {
    "BLOCKED — authorized live ingestion and manual acceptance remain"
} else {
    "BLOCKED — manual Windows/Ableton acceptance remains"
}

$NormalPath = "Not generated"
$SegmentedPaths = "Not generated"
if ($FixtureData) {
    $NormalPath = [string]$FixtureData.normal.paths[0]
    $SegmentedPaths = ($FixtureData.segmented.paths | ForEach-Object { [string]$_ }) -join "`n"
}

$GitCommit = "unknown"
if (Get-Command git -ErrorAction SilentlyContinue) {
    $CommitOutput = (& git rev-parse HEAD 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $CommitOutput) {
        $GitCommit = $CommitOutput
    }
}
$PythonVersion = (& $Python --version 2>&1 | Out-String).Trim()

$Report = @"
# Audio Archive — Windows v0.3 Acceptance Report

**Generated:** $((Get-Date).ToUniversalTime().ToString("o"))  
**Repository commit:** $GitCommit  
**Platform:** $([Environment]::OSVersion.VersionString)  
**Python:** $PythonVersion  
**Release status:** **$ReleaseStatus**

## Automated acceptance

| Check | Result |
| --- | --- |
| Toolchain readiness | $(Status-Text $Doctor.Passed) |
| Full pytest suite | $(Status-Text $Tests.Passed) |
| Existing archive integrity | $(Status-Text $ArchiveVerify.Passed) |
| Ableton fixture generation and verification | $(Status-Text $Fixtures.Passed) |
| Automated core | **$AutomatedStatus** |
| Authorized live complete ingestion | **$LiveStatus** |

### Authorized live ingestion

$LiveDetail

## Ableton files for manual acceptance

### Normal 32-bit float WAV

$NormalPath

### Forced-segmentation fixture

The following files were created through the same production segmentation code path using an acceptance-only small safe-size threshold:

~~~text
$SegmentedPaths
~~~

## Manual acceptance — must be completed by the user

- [ ] Double-click launch.cmd; Audio Archive opens in the default browser without requiring the user to open a terminal manually.
- [ ] Confirm the browser address is loopback-only (127.0.0.1 or another loopback address) and the queue is restored after restarting the app.
- [ ] Add an artist/title job in the browser and confirm queue/status behavior is understandable.
- [ ] Preview a CSV containing both valid and invalid rows and confirm valid rows can still be queued.
- [ ] Confirm an ambiguous source can be reviewed without blocking another runnable job.
- [ ] Open the normal WAV above in Ableton Live 12 and confirm it imports as expected.
- [ ] Open the segmented WAV files above in Ableton Live 12 in order and confirm the sequence is gapless.
- [ ] Confirm Warp and clip fades can remain disabled for a neutral transfer and no unexpected gain/transpose change is present.
- [ ] If an authorized live URL was supplied, inspect the completed item folder and confirm source master, Ableton WAV, MP3, source metadata, thumbnail, manifest, logs, and SHA256SUMS are present.

## Automated detail

### Doctor

~~~text
$($Doctor.Output)
~~~

### Tests

~~~text
$($Tests.Output)
~~~

### Archive verification

~~~text
$($ArchiveVerify.Output)
~~~

### Fixture generation

~~~text
$($Fixtures.Output)
~~~

## Release rule

The permanent archive must not begin until automated acceptance passes, one authorized live complete job passes when available, and every applicable manual checkbox above has been verified on the target Windows/Ableton machine.
"@

Set-Content -LiteralPath $ReportPath -Value $Report -Encoding UTF8
Write-Host ""
Write-Host "Acceptance report: $ReportPath"
Write-Host "Release status: $ReleaseStatus"

if (-not $CorePass -or $LiveStatus -eq "FAIL") {
    exit 1
}
exit 0
