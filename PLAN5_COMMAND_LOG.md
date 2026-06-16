# Plan 5 — Command Log

Every command for Plan 5 (same-day parity with predict_today). Paper-only.
Branch: `fix/sameday-parity-with-predict-today`.

## Phase 0 — prep

```
(Get-Location).Path             -> C:\Users\OMEN\Pictures\Polymarket\polyswarm
git rev-parse --abbrev-ref HEAD -> (was fix/wallet-open-position-atomic)
git status --short              -> clean except ?? agentdb.rvf / agentdb.rvf.lock (ruflo artifacts)
git log --oneline -6            -> f5e767a Plan4, 3310b9a Plan3, 353b0cc Plan2, 8ea3920 Plan1 (ALL committed)
git checkout -b fix/sameday-parity-with-predict-today  -> Switched to a new branch
```
Plans 1-4 committed -> safe to proceed. agentdb.rvf* untouched.

## Phase 1 — map predict_today vs sameday (Grep/Read, read-only)

Read predict_today.py, sameday.py (full), evidence_pack/loop.build_pack, challenger.ensemble_forecast,
core/swarm.forecast, profitability.ev_gate, risk_guards, journal, scanner.is_stale, and the related tests.

Gaps found:
  - _ai_scout forecast(swarm) + ensemble_forecast(challenger) ran with NO evidence; pack built AFTER (line ~354).
  - EV gate calls omitted m + confidence -> spread/liquidity/uncertainty/exit-risk penalties never ran.
  - divergence/consensus/evidence/sizer/event-portfolio/Guard-D skips were print+obs only (NOT journaled).
  - no explicit pre-forecast stale filter (relied on the post-forecast risk guard).
  - MiroFish launch-only, not labeled.
Confirmed: challenger.ensemble_forecast(question, market_odds, extra_context, models) ALREADY supports
context (Phase 4 fully achievable); scanner.is_stale(m) -> (bool, reason).

## Phases 2-10 — implementation + tests (no shell)

Edited harness/sameday.py: import scanner; _ai_scout now takes evidence_text and passes it to BOTH
Swarm.forecast(extra_context=) and challenger.ensemble_forecast(...); MiroFish launch labeled
mirofish_launched_not_used + health carries mirofish_used=False / evidence_used; place_sameday builds
the canonical pack BEFORE the forecast (build error -> no bet, never forecast blind), adds an explicit
scanner.is_stale filter, passes m+confidence to both EV gates, and routes EVERY no-bet branch through
_sd_skip (print+obs+journal) with sameday_-prefixed reasons. NEW harness/tests/test_sameday_parity.py
(21 cases).

## Phases 11-12 — test + static verification

```
# interpreter: .\.venv\Scripts\python.exe
python -m harness.tests.test_sameday_parity  -> 21/21 passed (exit 0)
python run_tests.py --no-llm                 -> SUMMARY: 59/59 modules passed (exit 0, no FAIL)
```
pytest not used (run_tests.py is the supported stdlib runner). NOT run (per constraints):
supervisor start/stop, live daemons, real betting loop, DB repair, live polyswarm.db.

Static (Grep, read-only) — harness/sameday.py:
```
extra_context=evidence_text         -> swarm forecast (line 191); challenger gets evidence_text (test-proven)
m=m, confidence=cons                -> BOTH EV gates (lines 418, 482)
scanner.is_stale                    -> explicit pre-forecast stale filter (line 309)
mirofish_launched_not_used / "mirofish_used": False  -> MiroFish honesty (lines 172/178/225)
_sd_skip(                           -> ALL no-bet branches journaled (divergence/consensus/evidence/
                                       stale/event/sizer/EV/risk/bankroll/exposure/wallet/observe-only)
wallet.open_position(               -> 2 inline-gated opens (lines 444, 517), each followed by fr.opened check
```
