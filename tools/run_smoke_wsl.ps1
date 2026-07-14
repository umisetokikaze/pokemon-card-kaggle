[CmdletBinding()]
param(
    [string]$Distribution = "Ubuntu-22.04"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$wslHome = (& wsl.exe -d $Distribution -- sh -lc 'printf %s "$HOME"').Trim()
$python = "$wslHome/.venvs/pokemon-card-kaggle/bin/python"

& wsl.exe -d $Distribution -- test -x $python
if ($LASTEXITCODE -ne 0) {
    throw "Run tools/setup_local.ps1 before the smoke test."
}

& wsl.exe -d $Distribution --cd $repoRoot -- $python tests/smoke_match.py
if ($LASTEXITCODE -ne 0) {
    throw "The local self-play smoke test failed."
}
