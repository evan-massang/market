# Plan 3 — Command Log

Every command for Plan 3 (gate shortcut betting paths). Paper-only.
Branch: `fix/gate-shortcut-betting-paths`.

## Phase 0 — prep

```
(Get-Location).Path             -> C:\Users\OMEN\Pictures\Polymarket\polyswarm
git rev-parse --abbrev-ref HEAD -> (was fix/swarm-degradation-safety)
git status --short              -> clean except ?? agentdb.rvf / agentdb.rvf.lock (ruflo artifacts)
git log --oneline -4            -> 353b0cc Plan2, 8ea3920 Plan1  (BOTH committed)
git checkout -b fix/gate-shortcut-betting-paths  -> Switched to a new branch
```
Plan 1 (8ea3920) + Plan 2 (353b0cc) committed -> safe to proceed. agentdb.rvf* untouched.

## Phase 1 — find every betting entrypoint (Grep/Read, read-only)

`grep "\.open_position\(|open_position\("` repo-wide. Production (non-test) callers:
```
harness/strategy_bet.py:76   favorite-longshot price rule -> open_position  (NO EV/risk/bankroll/exposure/health)  [SHORTCUT]
harness/loop.py:282          run_once -> open_position after size_bet        (NO safety stack; can bet fallback 0.5) [LEGACY]
harness/place_bet.py:101     manual single-market -> open_position           (NO safety stack)                      [SHORTCUT]
harness/predict_today.py:1015/1081  GATED (Plan 1+2 stack inline)            [ALLOWED]
harness/sameday.py:425/512          GATED (Plan 1+2 stack inline)            [ALLOWED]
harness/wallet.py:153        the def itself
```
Test-only callers: harness/test_wallet.py, harness/tests/test_{wallet,db_check,p7_wire,p8_wire,
settlement,settlement_idempotent,portfolio_guards,swarm_degradation}.py  [TEST-ONLY, allowed].

Launchers/scheduling:
```
harness_pass.bat:9   python -m harness.loop settle        (settlement-only, safe)
harness_pass.bat:10  python -m harness.strategy_bet --max 60   <-- SCHEDULES THE UNGATED SHORTCUT
services.py          supervisor runs ONLY: mirofish_backend, dashboard, sameday_daemon,
                     ai_pipeline(=predict_today daemon). NO strategy_bet / NO loop betting.
scripts/5_ai_pipeline.ps1  python -m harness.predict_today daemon ...  (gated path)
```
loop CLI: `run`->run_once(bets), `settle`->settle_resolved(safe), `daemon`->loops run_once.

## Phases 2-9 — implementation (no shell)

NEW harness/safe_bet.py (open_position_if_safe: swarm-health→EV→risk→bankroll→exposure→open
once; disabled-by-default switches; Plan-3 reason vocabulary). Edited harness/strategy_bet.py
(disabled by default via ENABLE_STRATEGY_BET; routes through safe_bet when enabled; no honest
prob -> no bet), harness/loop.py (run_once betting disabled by default via
ENABLE_LEGACY_LOOP_BETTING; fallback-prob blocked; routes opens through safe_bet; settle/score
kept), harness/place_bet.py (routes through safe_bet), harness_pass.bat (settlement-only by
default; strategy line commented), harness/config_check.py + .env.example (document the flags).
NEW harness/tests/test_shortcut_paths.py (14 cases).

## Phases 10-11 — test + static verification

```
# interpreter: .\.venv\Scripts\python.exe
python -m harness.tests.test_shortcut_paths    -> 14/14 passed (exit 0)
python -m harness.tests.test_config_check      -> 5/5 passed (exit 0)
python run_tests.py --no-llm                   -> SUMMARY: 57/57 modules passed (exit 0, no FAIL)
```
pytest not used (run_tests.py is the supported stdlib runner). NOT run (per constraints):
supervisor start/stop, live daemons, real betting loop, db_check --repair.

Static (Grep, read-only):
```
grep "wallet\.open_position\(" in harness/ (non-test)
  -> ONLY safe_bet.py (1, controlled) + predict_today.py (2, gated) + sameday.py (2, gated).
     strategy_bet / loop / place_bet no longer call it directly.
grep ENABLE_STRATEGY_BET / ENABLE_LEGACY_LOOP_BETTING
  -> safe_bet.py (switches), loop.py + strategy_bet.py (consumers), config_check + .env.example (docs).
```

