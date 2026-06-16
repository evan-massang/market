@echo off
REM Polymarket harness — scheduled pass. PAPER ONLY.
REM
REM By DEFAULT this pass ONLY settles resolved paper bets (safe; opens no positions).
REM Strategy betting is DISABLED BY DEFAULT (Plan 3): harness.strategy_bet self-exits
REM with 'strategy_bet_disabled_by_default' unless ENABLE_STRATEGY_BET=true, and even
REM when opted in every bet is EV/risk/bankroll/exposure-gated via harness.safe_bet.
REM The strategy line below is left COMMENTED so a scheduled pass never deploys ungated
REM bets into the shared wallet. Uncomment + set the env ONLY after review.
cd /d C:\Users\OMEN\Pictures\Polymarket\polyswarm
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo ===== PASS %DATE% %TIME% ===== >> harness_cron.log
.\.venv\Scripts\python.exe -m harness.loop settle >> harness_cron.log 2>&1
REM  --- opt-in, safety-gated strategy betting (DISABLED by default) ---
REM  set ENABLE_STRATEGY_BET=true
REM  .\.venv\Scripts\python.exe -m harness.strategy_bet --max 60 >> harness_cron.log 2>&1
echo ===== END  %DATE% %TIME% ===== >> harness_cron.log
