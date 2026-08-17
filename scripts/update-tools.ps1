$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "Windows Package Manager (winget) is required to update external tools."
}

foreach ($PackageId in @("DenoLand.Deno", "Gyan.FFmpeg")) {
    & winget upgrade --id $PackageId --exact --source winget `
        --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not update $PackageId (exit code $LASTEXITCODE)."
    }
}

& "$PSScriptRoot\setup.ps1" -SkipToolInstall -RefreshPortableTools
