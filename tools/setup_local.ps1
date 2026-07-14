[CmdletBinding()]
param(
    [string]$Distribution = "Ubuntu-22.04"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$uv = (& wsl.exe -d $Distribution -- bash -lc "command -v uv").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($uv)) {
    throw "uv was not found in WSL distribution $Distribution."
}

$wslHome = (& wsl.exe -d $Distribution -- sh -lc 'printf %s "$HOME"').Trim()
$venvPath = "$wslHome/.venvs/pokemon-card-kaggle"
if (-not $venvPath.StartsWith("$wslHome/.venvs/")) {
    throw "Refusing to create a virtual environment outside the WSL home directory."
}

& wsl.exe -d $Distribution -- test -x "$venvPath/bin/python"
if ($LASTEXITCODE -eq 0) {
    & wsl.exe -d $Distribution -- "$venvPath/bin/python" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) {
        & wsl.exe -d $Distribution -- $uv venv --clear --python 3.11 $venvPath
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to replace the WSL virtual environment."
        }
    }
}
else {
    & wsl.exe -d $Distribution -- $uv venv --python 3.11 $venvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the WSL virtual environment."
    }
}

& wsl.exe -d $Distribution --cd $repoRoot -- $uv pip install --python "$venvPath/bin/python" -r requirements-local.txt
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install local simulation dependencies."
}

Write-Host "PASS: WSL environment is ready at $venvPath"
