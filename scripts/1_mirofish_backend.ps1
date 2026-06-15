$host.UI.RawUI.WindowTitle = "1) MiroFish Backend :5001"
$env:PYTHONIOENCODING = "utf-8"
Set-Location "C:\Users\OMEN\Pictures\Polymarket\MiroFish\backend"
Write-Host "Starting MiroFish backend (Flask :5001)..." -ForegroundColor Green
& "C:\Users\OMEN\Pictures\Polymarket\MiroFish\backend\.venv\Scripts\python.exe" run.py
