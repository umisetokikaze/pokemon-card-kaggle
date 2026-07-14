[CmdletBinding()]
param(
    [string]$OutputPath = "dist/submission.tar.gz"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
foreach ($required in @("main.py", "deck.csv", "cg")) {
    $source = Join-Path $repoRoot $required
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Required source is missing: $source"
    }
}

if (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $repoRoot $OutputPath
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

& tar -czf $OutputPath -C $repoRoot "main.py" "deck.csv" "cg"
if ($LASTEXITCODE -ne 0) {
    throw "tar failed to build the submission archive."
}

& (Join-Path $PSScriptRoot "validate_submission.ps1") -ArchivePath $OutputPath

