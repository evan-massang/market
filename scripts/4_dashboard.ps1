$host.UI.RawUI.WindowTitle = "4) Dashboard :8800"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:DASH_STREAM_LOG = "ai_night.log"   # live feed follows the AI pipeline (find->gather->MiroFish->LLM->bet)
Set-Location "C:\Users\OMEN\Pictures\Polymarket\polyswarm"
Write-Host "Starting dashboard (http://localhost:8800)..." -ForegroundColor Yellow
& "C:\Users\OMEN\Pictures\Polymarket\polyswarm\.venv\Scripts\python.exe" -m harness.dashboard
