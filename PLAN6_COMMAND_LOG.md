# Plan 6 — Command Log

Every command for Plan 6 (event portfolio / multi-bet event safety). Paper-only.
Branch: `fix/event-portfolio-safety`.

## Phase 0 — prep

```
(Get-Location).Path             -> C:\Users\OMEN\Pictures\Polymarket\polyswarm
git rev-parse --abbrev-ref HEAD -> (was fix/sameday-parity-with-predict-today)
git status --short              -> clean except ?? agentdb.rvf / agentdb.rvf.lock (ruflo artifacts)
git log --oneline -6            -> 29b90e3 Plan5, f5e767a Plan4, 3310b9a Plan3, 353b0cc Plan2, 8ea3920 Plan1 (ALL committed)
git checkout -b fix/event-portfolio-safety  -> Switched to a new branch
```
Plans 1-5 committed -> safe to proceed. agentdb.rvf* untouched.

## Phase 1 — inspect event-portfolio paths

Work-list (Glob/Grep): harness/event_portfolio.py (core engine; uses risk-free/arbitrage/guaranteed),
predict_today.build_event_legs/run_event_portfolio/event_leg_reject_reason + the multi-leg path in
predict_one, sameday event path, portfolio_guards.py (correlation/event), tests/test_event_portfolio.py.
A parallel understanding workflow (map-event-portfolio) deep-read all of these; findings recorded in the report.

A parallel UNDERSTANDING workflow (4 read-only analysts, run wf_20d7b9dc) deep-read
event_portfolio.py + predict_today/sameday event paths + tests + portfolio_guards and
CONFIRMED: the engine is PURE (never opens), the "arbitrage" basket is multi-leg with NO
atomicity guarantee ("if the consumer opens one leg, the guaranteed N-1 payoff is destroyed
and the position is just a single naked NO with full downside"), ME/exhaustiveness is
assumed-never-verified, no staleness handling, partial-basket execution is the core risk.

## Phases 2-10 — implementation (no shell)

NEW harness/event_safety.py (strict labels + reject reasons + validate_event_legset +
check_event_position_coherence + classify_event_execution; multi-leg execution OFF by default).
Edited harness/event_portfolio.py (softened "risk-free/arbitrage" prose to recommendation-only;
engine logic + is_arbitrage flag UNCHANGED so the 7 engine tests still pass). Edited
harness/predict_today.py (import event_safety; run_event_portfolio now classifies execution ->
forces my_pos=None for arb/incoherent baskets + coherence check; event_leg_reject_reason surfaces
the exact code). These flow to sameday automatically (it calls run_event_portfolio +
event_leg_reject_reason). Edited harness/db_check.py (event_multiple_open_yes check).
NEW harness/tests/test_event_safety.py (22 cases).

## Phase 11-12 — test + static verification

```
# interpreter: .\.venv\Scripts\python.exe
python -m harness.tests.test_event_safety       -> 22/22 passed (exit 0)
python -m harness.tests.test_event_portfolio    -> (in full run) passed (engine recommendation intact)
python run_tests.py --no-llm                    -> SUMMARY: 60/60 modules passed (exit 0, no FAIL)
```
A second ADVERSARIAL workflow (3 skeptics, run wf_591dd7ff) tried to break the guarantees
(open an arb leg / stack a 2nd YES / bypass Plan 1-5 / emit an executed risk-free claim):
**0 holes found**, 2 low-severity caveats surfaced -> both HARDENED:
  (1) arb execution now blocked UNCONDITIONALLY (env flag can't enable single-leg arb);
  (2) coherence check now FAIL-CLOSED (was except:pass).
Re-ran: test_event_safety 24/24, full suite 60/60 (exit 0). pytest not used. NOT run (per
constraints): supervisor start/stop, live daemons, real betting loop, DB repair, live polyswarm.db.

