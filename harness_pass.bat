@echo off
REM Polymarket harness — continuous +EV pass. PAPER ONLY. Settles resolved bets,
REM then redeploys freed capital into fresh favorite-longshot +EV bets (the only
REM strategy with a backtested, slippage-robust edge). Logs to harness_cron.log.
cd /d C:\Users\OMEN\Pictures\Polymarket\polyswarm
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo ===== PASS %DATE% %TIME% ===== >> harness_cron.log
.\.venv\Scripts\python.exe -m harness.loop settle >> harness_cron.log 2>&1
.\.venv\Scripts\python.exe -m harness.strategy_bet --max 60 >> harness_cron.log 2>&1
echo ===== END  %DATE% %TIME% ===== >> harness_cron.log
