# polymarket-ai.ps1 — thin wrapper around the Python supervisor (single source of truth).
# Usage:
#   .\polymarket-ai.ps1 start      start the whole Polymarket AI in the background
#   .\polymarket-ai.ps1 status     show every service + health
#   .\polymarket-ai.ps1 stop       stop everything cleanly
#   .\polymarket-ai.ps1 restart    restart the full system
#   .\polymarket-ai.ps1 logs       recent logs from all services
#   .\polymarket-ai.ps1 doctor     readiness check before starting
#   .\polymarket-ai.ps1 tail dashboard       follow one service's log
#   .\polymarket-ai.ps1 start ai_pipeline    start a single service
# PAPER-ONLY. This script does NOT duplicate any logic — it just calls the supervisor.

param(
  [Parameter(Position = 0)][string]$Command = "status",
  [Parameter(Position = 1)][string]$Service = ""
)

# Resolve the polyswarm package dir whether this script lives inside it or one level up.
if (Test-Path (Join-Path $PSScriptRoot "harness\supervisor.py")) {
  $Poly = $PSScriptRoot
} else {
  $Poly = Join-Path $PSScriptRoot "polyswarm"
}

$Py = Join-Path $Poly ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }   # fall back to PATH python

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location $Poly

if ([string]::IsNullOrWhiteSpace($Service)) {
  & $Py -m harness.supervisor $Command
} else {
  & $Py -m harness.supervisor $Command $Service
}
exit $LASTEXITCODE
