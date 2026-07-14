[CmdletBinding()]
param(
    [string]$ArchivePath = "dist/submission.tar.gz"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not [System.IO.Path]::IsPathRooted($ArchivePath)) {
    $ArchivePath = Join-Path $repoRoot $ArchivePath
}
$ArchivePath = [System.IO.Path]::GetFullPath($ArchivePath)

if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
    throw "Submission archive was not found: $ArchivePath"
}

$archive = Get-Item -LiteralPath $ArchivePath
$maxBytes = [int64][Math]::Floor(197.7 * 1MB)
if ($archive.Length -gt $maxBytes) {
    throw "Submission exceeds the 197.7 MiB limit: $($archive.Length) bytes"
}

$entries = @(& tar -tzf $ArchivePath)
if ($LASTEXITCODE -ne 0) {
    throw "tar could not read the submission archive."
}

foreach ($entry in $entries) {
    if ([string]::IsNullOrWhiteSpace($entry)) {
        continue
    }

    $parts = $entry -split "/"
    if ($entry.StartsWith("/") -or $entry.Contains("\") -or $parts -contains "..") {
        throw "Unsafe archive path: $entry"
    }
}

if ($entries | Where-Object { $_ -like "*/__pycache__/*" -or $_ -like "*.pyc" }) {
    throw "The submission archive must not contain Python bytecode caches."
}

foreach ($required in @("main.py", "deck.csv")) {
    if ($entries -notcontains $required) {
        throw "$required must exist at the top level of the archive."
    }
}

if (-not ($entries | Where-Object { $_ -like "cg/*" })) {
    throw "The cg engine package is missing from the archive."
}

if (-not ($entries | Where-Object { $_ -like "ptcg_policy/*" })) {
    throw "The ptcg_policy package is missing from the archive."
}

$mainText = ((& tar -xOf $ArchivePath "main.py") -join "`n")
if ($LASTEXITCODE -ne 0 -or $mainText -notmatch "(?m)^def\s+agent\s*\(") {
    throw "main.py does not define an agent function."
}

$deckRaw = @(& tar -xOf $ArchivePath "deck.csv")
if ($LASTEXITCODE -ne 0) {
    throw "deck.csv could not be read from the archive."
}
$deckLines = @($deckRaw | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($deckLines.Count -ne 60) {
    throw "deck.csv must contain exactly 60 non-empty lines. Found: $($deckLines.Count)"
}

foreach ($line in $deckLines) {
    $cardId = 0
    if (-not [int]::TryParse($line.Trim(), [ref]$cardId) -or $cardId -lt 0) {
        throw "deck.csv contains an invalid card ID: $line"
    }
}

Write-Host "PASS: $ArchivePath"
Write-Host "  Size: $($archive.Length) bytes"
Write-Host "  Deck: 60 cards"
Write-Host "  Entry point: main.py::agent"
