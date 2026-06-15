$host.UI.RawUI.WindowTitle = "3) Sameday Daemon (the bot)"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location "C:\Users\OMEN\Pictures\Polymarket\polyswarm"
Write-Host "Starting sameday trading daemon (logs -> sameday_live.log)..." -ForegroundColor Magenta
& "C:\Users\OMEN\Pictures\Polymarket\polyswarm\.venv\Scripts\python.exe" -u -m harness.sameday daemon 2> sameday_err.log | Tee-Object sameday_live.log
