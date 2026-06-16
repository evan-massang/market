# Plan 1 — Command Log

Every command run for Plan 1 (fail-closed money gates), in order. Paper-only.
Branch: `fix/fail-closed-money-gates`.

## Phase 0 — safety prep

```
(Get-Location).Path
  -> C:\Users\OMEN\Pictures\Polymarket\polyswarm

git status --short
  -> ?? agentdb.rvf
  -> ?? agentdb.rvf.lock        (pre-existing ruflo artifacts, untracked, not mine)

git rev-parse --abbrev-ref HEAD
  -> main

git remote -v
  -> origin  https://github.com/evan-massang/market.git

git checkout -b fix/fail-closed-money-gates
  -> Switched to a new branch 'fix/fail-closed-money-gates'
```

NOTE: This is the single repo the live services run from — there is no separate
physical "audit copy". Services were already STOPPED. All work isolated on the
fix branch; live DB (polyswarm.db) untouched; no service start/stop/restart.

## Phase 1 — locate fail-open paths (Grep, read-only)

Searched harness/ for the four wrappers + every fail-open token. Read in full:
predict_today.py (wrappers 86-171 + call sites 850-1009 + _skip 635), sameday.py
(354-507 gate mirror + _sd_skip 205), risk_guards.py, bankroll.py, market_quality.py,
portfolio_guards.py, profitability.py, wallet.py(get_state), and the existing tests
test_p7_wire / test_p8_wire / test_bankroll / test_market_quality / test_acceptance.

FAIL-OPEN sites identified (each returns ALLOW on fault):
  predict_today._p7_ev_gate      L95  return True,"ev_gate_unavailable"   ; L114 return True,"ev_gate_error"
  predict_today._p8_risk_guards  L127 return True,"risk_guards_unavailable"; L130 .get("allow",True); L137 return True,"risk_guards_error"
  predict_today._p9_can_trade    L154 return True,"can_trade_error"
  predict_today._p9_exposure_ok  L171 return True,"exposure_error"
  risk_guards.evaluate           L96  except -> {"allow": True, ...}
  market_quality.check_stale_price L89 except -> (True,None,...)  (+ check_liquidity L112, check_spread L137)
  bankroll.can_trade             L139 except -> True,"can_trade_error"  (+ silent: dead DB via drawdown_state swallow -> True,"ok")
  bankroll.exposure_ok           L183 bankroll<=0 -> True ; L193 except -> True,None,{}

Tests that ASSERT fail-open (must flip): test_p7_wire.test_ev_gate_unavailable_falls_back_to_allow;
test_p8_wire.test_fail_open_on_internal_error + test_predict_today_wrapper_allows_clean_and_fails_open;
test_bankroll.test_never_raises_on_missing_table.

Non-test callers of these gates: predict_today.py, sameday.py, risk_guards.py, market_quality.py ONLY.
(strategy_bet does NOT call them — its bypass is Plan 2, out of scope.)

## Phases 2-10 — implementation + tests (commands)

Code edited (no shell): created harness/safety_gate.py; edited harness/predict_today.py
(4 wrappers + module-level _risk_guards/_bankroll), harness/sameday.py (8 money-gate
branches -> _sd_skip), harness/risk_guards.py (evaluate except -> block),
harness/market_quality.py (3 check_* excepts -> block), harness/bankroll.py
(can_trade readable-baseline precondition + exposure_ok no-bankroll block + flipped excepts).
Tests: new harness/tests/test_fail_closed_gates.py; flipped fail-open asserts in
test_p7_wire / test_p8_wire / test_bankroll; added error-blocks test in test_market_quality.

## Phase 11 — test commands + results

```
# interpreter: .\.venv\Scripts\python.exe
python -m harness.tests.test_fail_closed_gates        -> 24/24 passed (exit 0)
python -m harness.tests.test_p7_wire                  -> 7/7 passed (exit 0)
python -m harness.tests.test_p8_wire                  -> 8/8 passed (exit 0)
python -m harness.tests.test_bankroll                 -> 12/12 passed (exit 0)
python -m harness.tests.test_market_quality           -> 12/12 passed (exit 0)
python -m harness.tests.test_acceptance               -> 18/18 passed (exit 0)
python run_tests.py --no-llm                          -> SUMMARY: 55/55 modules passed (exit 0)
```
pytest was NOT used (the suite is a stdlib runner; run_tests.py is the supported path).
NOT run (per constraints): supervisor start/stop, live daemons, real betting loop, db_check --repair.

## Phase 12 — static verification (Grep, read-only)

```
grep "ev_gate_unavailable"|"ev_gate_error"|"risk_guards_unavailable"|... in harness/
  -> only test docstrings mention "fail-open" (historical text); no live gate uses the old bare reasons.
grep "return True"|"allow": True in {predict_today,risk_guards,market_quality,bankroll,safety_gate}.py
  -> all remaining are legitimate POST-EVALUATION allows (see report Phase-12 table).
grep "_fail_closed" in lower-level gate modules -> bankroll(2), market_quality(1), risk_guards(1);
  predict_today uses the safety_gate.* constants.
```

## Close-out verification (review task)

```
git rev-parse --abbrev-ref HEAD          -> fix/fail-closed-money-gates
git status --short                       -> 9 M (harness code+tests) + 4 ?? (safety_gate.py,
                                            test_fail_closed_gates.py, PLAN1_*.md) + 2 ?? ruflo (agentdb.rvf*)
git diff --stat                          -> 9 files changed, +245 / -163 (modified tracked only)
python run_tests.py --no-llm             -> SUMMARY: 55/55 modules passed (exit 0, no FAIL) [run twice, both green]
python -m harness.tests.test_fail_closed_gates  -> 24/24 (exit 0)
python -m harness.tests.test_p7_wire / test_p8_wire / test_bankroll / test_market_quality / test_acceptance
                                         -> 7/7, 8/8, 12/12, 12/12, 18/18 (all exit 0)
```
NOT committed (awaiting user review). agentdb.rvf* left untouched (pre-existing ruflo artifacts).
```


