$host.UI.RawUI.WindowTitle = "5) AI Pipeline (find->gather->MiroFish->LLM->bet)"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# decode Python's UTF-8 stdout correctly (else em-dashes / Türkiye / Côte d'Ivoire mojibake in the feed)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Set-Location "C:\Users\OMEN\Pictures\Polymarket\polyswarm"
$log = "C:\Users\OMEN\Pictures\Polymarket\polyswarm\ai_night.log"
# fresh UTF-8 log (no BOM) so the dashboard SSE feed reads it cleanly
[System.IO.File]::WriteAllText($log, "", (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Starting the all-night AI pipeline: FIND -> GATHER -> MiroFish REPORT -> LLM -> DECIDE" -ForegroundColor Green
Write-Host "(one deep, MiroFish-backed forecast per cycle; slow on CPU by design)" -ForegroundColor DarkGray
# show in this window AND append a UTF-8 log the dashboard tails (PS5.1 Tee-Object writes UTF-16, which the feed can't read)
& "C:\Users\OMEN\Pictures\Polymarket\polyswarm\.venv\Scripts\python.exe" -u -m harness.predict_today daemon `
    --with-mirofish --size 5 --rounds 1 --min-edge 0.03 --interval 30 --mf-wait 360 2>&1 |
  ForEach-Object {
    Write-Host $_
    [System.IO.File]::AppendAllText($log, ($_ -as [string]) + "`n", (New-Object System.Text.UTF8Encoding($false)))
  }
