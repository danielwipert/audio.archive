param(
    [switch]$SkipToolInstall,
    [switch]$RefreshPortableTools
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ToolsDirectory = Join-Path $ProjectRoot "tools"
Set-Location $ProjectRoot

function Refresh-ProcessPath {
    $MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$MachinePath;$UserPath"
}

function Find-ToolSource([string]$CommandName) {
    $Command = Get-Command $CommandName -CommandType Application -ErrorAction SilentlyContinue
    if ($Command) {
        return $Command.Source
    }
    return $null
}

function Install-WingetPackage([string]$PackageId) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Windows Package Manager (winget) is required to install $PackageId."
    }

    & winget install --id $PackageId --exact --source winget `
        --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install $PackageId (exit code $LASTEXITCODE)."
    }
    Refresh-ProcessPath
}

function Copy-PortableTool(
    [string]$CommandName,
    [string]$PackageId,
    [string]$DestinationName
) {
    $Destination = Join-Path $ToolsDirectory $DestinationName
    if ((Test-Path $Destination) -and (-not $RefreshPortableTools)) {
        Write-Host "Using bundled $DestinationName"
        return
    }

    $Source = Find-ToolSource $CommandName
    if ((-not $Source) -and (-not $SkipToolInstall)) {
        Install-WingetPackage $PackageId
        $Source = Find-ToolSource $CommandName
    }
    if (-not $Source) {
        throw "$CommandName was not found after installing $PackageId. Open a new PowerShell window and run setup again."
    }

    Copy-Item -LiteralPath $Source -Destination $Destination -Force
    Write-Host "Bundled $DestinationName from $Source"
}

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python 3.11 or newer is required. Install Python, then run setup again."
}

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.11 or newer is required."
}

New-Item -ItemType Directory -Path $ToolsDirectory -Force | Out-Null

if (-not (Test-Path ".venv")) {
    py -3 -m venv .venv
}

& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -e ".[dev]"

Copy-PortableTool "deno.exe" "DenoLand.Deno" "deno.exe"

$BundledFfmpeg = Join-Path $ToolsDirectory "ffmpeg.exe"
$BundledFfprobe = Join-Path $ToolsDirectory "ffprobe.exe"
if ($RefreshPortableTools -or (-not (Test-Path $BundledFfmpeg)) -or (-not (Test-Path $BundledFfprobe))) {
    $FfmpegSource = Find-ToolSource "ffmpeg.exe"
    $FfprobeSource = Find-ToolSource "ffprobe.exe"
    if ((-not $FfmpegSource -or -not $FfprobeSource) -and (-not $SkipToolInstall)) {
        Install-WingetPackage "Gyan.FFmpeg"
        $FfmpegSource = Find-ToolSource "ffmpeg.exe"
        $FfprobeSource = Find-ToolSource "ffprobe.exe"
    }
    if (-not $FfmpegSource -or -not $FfprobeSource) {
        throw "ffmpeg.exe and ffprobe.exe were not found after installing Gyan.FFmpeg. Open a new PowerShell window and run setup again."
    }
    Copy-Item -LiteralPath $FfmpegSource -Destination $BundledFfmpeg -Force
    Copy-Item -LiteralPath $FfprobeSource -Destination $BundledFfprobe -Force
    Write-Host "Bundled FFmpeg and FFprobe"
}

& .venv\Scripts\python.exe -m audio_archive init
& .venv\Scripts\python.exe -m audio_archive doctor
if ($LASTEXITCODE -ne 0) {
    throw "Audio Archive setup finished, but the readiness check failed. Review the diagnostics above."
}

Write-Host "Audio Archive toolchain is ready."
