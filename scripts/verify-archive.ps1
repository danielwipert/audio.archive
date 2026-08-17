param(
    [string]$VideoId,
    [switch]$All
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Audio Archive is not set up. Run scripts\setup.ps1 first."
}
if ($All -and $VideoId) {
    throw "Specify either -VideoId or -All, not both."
}

if ($VideoId) {
    & $Python -m audio_archive verify $VideoId
} else {
    & $Python -m audio_archive verify --all
}
if ($LASTEXITCODE -ne 0) {
    throw "Archive verification failed. Review the item-level errors above."
}
