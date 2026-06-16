# start-polymarket-ai.ps1 — human-friendly one-liner: start the whole system.
# Equivalent to: .\polymarket-ai.ps1 start
& (Join-Path $PSScriptRoot "polymarket-ai.ps1") start
exit $LASTEXITCODE
