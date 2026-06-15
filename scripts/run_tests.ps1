<#
.SYNOPSIS
    Windows wrapper for the Polymarket harness no-network test suite.

.DESCRIPTION
    Runs polyswarm/run_tests.py with the project venv interpreter, PYTHONUTF8=1,
    from the polyswarm/ directory. Forwards any extra args (e.g. -v, --llm).
    Exits with the runner's exit code (non-zero on any failure).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1
    powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -v
    powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 --llm
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Args
)

$ErrorActionPreference = "Stop"

# polyswarm/ is the parent of this scripts/ directory.
$Root = Split-Path -Parent $PSScriptRoot

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Warning "venv python not found at $Python; falling back to 'python' on PATH"
    $Python = "python"
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Push-Location $Root
try {
    & $Python "run_tests.py" @Args
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $code
