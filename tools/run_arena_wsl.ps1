[CmdletBinding()]
param(
    [string]$Champion = "main.py",
    [string]$ChampionDeck = "deck.csv",
    [string]$Challenger = "main.py",
    [string]$ChallengerDeck = "deck.csv",
    [ValidateRange(1, 100000)]
    [int]$Pairs = 1,
    [string]$Matchup = "unspecified",
    [ValidateRange(0, 600000)]
    [int]$ActionTimeoutMs = 1000,
    [ValidateRange(1, 86400)]
    [int]$GameTimeoutSeconds = 900,
    [int]$Seed = 0,
    [string]$OutputPath,
    [string]$Distribution = "Ubuntu-22.04",
    [switch]$DebugEngine
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Convert-ToRepoRelativePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [bool]$MustExist
    )

    $fullPath = if ([System.IO.Path]::IsPathRooted($Path)) {
        [System.IO.Path]::GetFullPath($Path)
    }
    else {
        [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Path))
    }
    $relative = [System.IO.Path]::GetRelativePath($repoRoot, $fullPath)
    if ($relative -eq ".." -or $relative.StartsWith("..$([System.IO.Path]::DirectorySeparatorChar)")) {
        throw "Arena paths must stay inside the repository: $fullPath"
    }
    if ($MustExist -and -not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "Arena input does not exist: $fullPath"
    }
    return $relative.Replace('\', '/')
}

$championArg = Convert-ToRepoRelativePath -Path $Champion -MustExist $true
$championDeckArg = Convert-ToRepoRelativePath -Path $ChampionDeck -MustExist $true
$challengerArg = Convert-ToRepoRelativePath -Path $Challenger -MustExist $true
$challengerDeckArg = Convert-ToRepoRelativePath -Path $ChallengerDeck -MustExist $true

$wslHome = (& wsl.exe -d $Distribution -- sh -lc 'printf %s "$HOME"').Trim()
$python = "$wslHome/.venvs/pokemon-card-kaggle/bin/python"
& wsl.exe -d $Distribution -- test -x $python
if ($LASTEXITCODE -ne 0) {
    throw "Run tools/setup_local.ps1 before the arena."
}

$arenaArgs = @(
    "tools/arena.py",
    "--champion", $championArg,
    "--champion-deck", $championDeckArg,
    "--challenger", $challengerArg,
    "--challenger-deck", $challengerDeckArg,
    "--pairs", $Pairs,
    "--matchup", $Matchup,
    "--action-timeout-ms", $ActionTimeoutMs,
    "--game-timeout-seconds", $GameTimeoutSeconds,
    "--seed", $Seed
)
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $outputArg = Convert-ToRepoRelativePath -Path $OutputPath -MustExist $false
    $arenaArgs += @("--output", $outputArg)
}
if ($DebugEngine) {
    $arenaArgs += "--debug-engine"
}

& wsl.exe -d $Distribution --cd $repoRoot -- $python @arenaArgs
if ($LASTEXITCODE -ne 0) {
    throw "The paired arena reported one or more faults. Inspect the JSON report."
}
