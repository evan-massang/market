# Plan 1 — Fail-Closed Money Gates Report

> Goal: the bot can NEVER place a paper bet when EV, risk, bankroll, or exposure
> safety checks are unavailable, broken, errored, or returning invalid data.
> Safety gate unavailable / error / unknown = UNSAFE = NO BET.
>
> Paper-only. No real-money execution. No live-service or live-DB changes.
> Branch: `fix/fail-closed-money-gates`.

Status: **COMPLETE** — committed as 8ea3920.

## Gate map (Phase 1)

| Gate | File | Current failure behavior (FAIL-OPEN) | New behavior (FAIL-CLOSED) |
| ---- | ---- | ------------------------ | ------------ |
| `_p7_ev_gate` | predict_today.py | module None → `True,"ev_gate_unavailable"`; exception → `True,"ev_gate_error"` | module None → `False,"ev_gate_unavailable_fail_closed"`; bad price/prob/side → `False,"ev_gate_invalid_fail_closed"`; exception → `False,"ev_gate_error_fail_closed"`; malformed result → `False,"ev_gate_invalid_fail_closed"` |
| `_p8_risk_guards` | predict_today.py | import fail → `True,"risk_guards_unavailable"`; `.get("allow",True)`; exception → `True,"risk_guards_error"` | import fail → `False,"risk_guards_unavailable_fail_closed"`; exception → `False,"risk_guards_error_fail_closed"`; non-dict/missing allow → `False,"risk_guards_invalid_fail_closed"`; only explicit `allow is True` passes |
| `_p9_can_trade` | predict_today.py | exception → `True,"can_trade_error"` | import fail → `False,"bankroll_unavailable_fail_closed"`; exception → `False,"bankroll_error_fail_closed"`; malformed → `False,"bankroll_invalid_fail_closed"` |
| `_p9_exposure_ok` | predict_today.py | exception → `True,"exposure_error"` | import/scoreboard fail → `False,"exposure_unavailable_fail_closed"`; exception → `False,"exposure_error_fail_closed"`; malformed → `False,"exposure_invalid_fail_closed"` |
| `risk_guards.evaluate` | risk_guards.py | internal exception → `{"allow": True}` | internal exception → `{"allow": False,"blocking_reason":"risk_guards_internal_error_fail_closed"}` |
| `market_quality.check_*` | market_quality.py | check exception → `(True,None,…)` (fail-open) | check exception → `(False,"market_quality_error_fail_closed",…)` |
| `bankroll.can_trade` | bankroll.py | dead DB (swallowed) → `True,"ok"`; exception → `True,"can_trade_error"` | unreadable/uninitialized wallet → `False,"bankroll_unavailable_fail_closed"`; exception → `False,"bankroll_error_fail_closed"` |
| `bankroll.exposure_ok` | bankroll.py | `bankroll<=0` → allow; exception → `True,None,{}` | no positive bankroll baseline → `False,"exposure_invalid_fail_closed"`; exception → `False,"exposure_error_fail_closed"` |

A new module `harness/safety_gate.py` holds the canonical fail-closed reason
vocabulary + a `coerce()` validator (only an explicit `allow is True` passes; any
malformed/None/exception path blocks) so every gate speaks one language.

Status: **COMPLETE** — 55/55 test modules pass (incl. the new Plan-1 module).

## Summary

The four money gates (EV, risk, bankroll kill-switch, exposure cap) previously
**failed OPEN**: a missing module, an exception, a malformed result, or an
unreadable/locked DB returned `allow=True`, so a broken safety check silently let
a paper bet through. Plan 1 makes every one **fail CLOSED**: an unavailable /
errored / malformed / unknown safety check now returns `allow=False` with a
specific `*_fail_closed` reason that is logged and journaled, and the bet is
withheld (observe-only). The forecast itself is still computed + logged + frozen;
only the BET is blocked.

The single architectural lever: both `predict_today` bet paths AND both `sameday`
bet paths call the **same four wrapper functions** in `predict_today.py`, and every
call site already does `if not ok: skip/continue` before `wallet.open_position`.
Hardening those four wrappers (plus the two lower-level functions they call) makes
all four bet paths fail closed.

## Files Changed

| File | Purpose |
| ---- | ------- |
| `harness/safety_gate.py` (NEW) | Canonical fail-closed reason constants, `coerce()` (only explicit `True` passes), `finite()`, `is_fail_closed()`, `log_error()`. Imports nothing from harness at load (no cycle). |
| `harness/predict_today.py` | The 4 wrappers `_p7_ev_gate / _p8_risk_guards / _p9_can_trade / _p9_exposure_ok` rewritten to fail closed; added module-level guarded `_risk_guards` / `_bankroll` imports (mirroring `_profitability`) so "module unavailable" is detectable + testable. |
| `harness/sameday.py` | The 8 money-gate failure branches (EV/risk/bankroll/exposure × event-path + regular-path) now route through `_sd_skip` → print + `obs.on_trade_skip` + `journal.record_decision`. |
| `harness/risk_guards.py` | `evaluate()` internal-error `except` flipped from `allow=True` to `allow=False` + `blocking_reason="risk_guards_internal_error_fail_closed"` (+ exc detail). |
| `harness/market_quality.py` | The 3 check_* (`stale/liquidity/spread`) `except` branches flipped from fail-open `(True,…)` to fail-closed `(False,"market_quality_error_fail_closed",…)`. |
| `harness/bankroll.py` | `can_trade()` now reads `wallet.get_state()` DIRECTLY (raises on dead DB) + requires a positive `starting_bankroll` before trusting downstream; `except` → block. `exposure_ok()` blocks on no positive bankroll baseline + bad inputs; `except` → block. |
| tests | NEW `test_fail_closed_gates.py` (24 cases); flipped fail-open asserts in `test_p7_wire`, `test_p8_wire`, `test_bankroll`; added `check_error_fails_closed_not_open` in `test_market_quality`. |

## Behavior changed

- EV/risk/bankroll/exposure gates return `allow=False` (not True) on: module
  missing, exception, malformed/None result, invalid input.
- A **dead/locked/uninitialized wallet DB** now BLOCKS (`can_trade` + `exposure_ok`)
  instead of reading as a healthy empty book. (Previously `drawdown_state` swallowed
  the DB error into zeros and `can_trade` returned `(True,"ok")`.)
- `risk_guards.evaluate` and `market_quality` checks BLOCK on internal error
  (previously they failed open to "allow"). A network/parse hiccup can no longer
  look like "market quality OK".
- sameday money-gate skips are now **journaled** (decisions table), not only printed.
- The happy path is unchanged: a healthy +EV bet in a clean, liquid market with a
  readable, healthy wallet still passes all four gates; normal tightening blocks
  (`neg_ev_after_costs`, `drawdown_pause`, `high_spread`, `theme_exposure_cap`, …)
  keep their own non-fail-closed reasons.

## Fixed Gates

| Gate | Before (fault → result) | After (fault → result) | Files |
| ---- | ----------------------- | ---------------------- | ----- |
| EV (`_p7_ev_gate`) | unavailable/error → ALLOW | unavailable/error/invalid/malformed → BLOCK | predict_today.py |
| Risk (`_p8_risk_guards`) | import/error → ALLOW; `.get("allow",True)` | unavailable/error/invalid → BLOCK; only explicit `True` allows | predict_today.py |
| Risk core (`risk_guards.evaluate`) | internal error → `allow=True` | internal error → `allow=False` | risk_guards.py |
| Market quality (`check_*`) | error → `(True,…)` | error → `(False,"market_quality_error_fail_closed",…)` | market_quality.py |
| Bankroll (`_p9_can_trade` / `can_trade`) | error/dead-DB → ALLOW | unavailable/error/invalid/uninitialized → BLOCK | predict_today.py, bankroll.py |
| Exposure (`_p9_exposure_ok` / `exposure_ok`) | error/no-bankroll → ALLOW | unavailable/error/invalid → BLOCK | predict_today.py, bankroll.py |

## New Block Reasons

`ev_gate_unavailable_fail_closed`, `ev_gate_error_fail_closed`,
`ev_gate_invalid_fail_closed`, `risk_guards_unavailable_fail_closed`,
`risk_guards_error_fail_closed`, `risk_guards_invalid_fail_closed`,
`risk_guards_internal_error_fail_closed`, `market_quality_error_fail_closed`,
`bankroll_unavailable_fail_closed`, `bankroll_error_fail_closed`,
`bankroll_invalid_fail_closed`, `exposure_unavailable_fail_closed`,
`exposure_error_fail_closed`, `exposure_invalid_fail_closed`.
(All defined once in `harness/safety_gate.py`; `is_fail_closed()` recognizes the
`_fail_closed` suffix.)

## Tests Added / changed

NEW `harness/tests/test_fail_closed_gates.py` (24): EV unavailable/raises/invalid/
malformed; risk unavailable/raises/malformed/allow-not-True; bankroll
unavailable/raises/malformed; exposure unavailable/raises/malformed;
risk_guards internal exception; can_trade & exposure DB-unavailable; uninitialized
wallet; `_skip`/`_sd_skip` record a no-bet decision and never open a position; a
**structural scan** proving every gate call site in predict_today.py + sameday.py is
immediately consumed by an `if not <ok>:` guard before any `wallet.open_position`;
happy-path-allows; normal-blocks-keep-their-own-reasons; `safety_gate.coerce` unit.

Flipped to fail-closed: `test_p7_wire.test_ev_gate_unavailable_fails_closed`,
`test_p8_wire.test_fail_closed_on_internal_error` +
`test_predict_today_wrapper_allows_clean_and_fails_closed`,
`test_bankroll.test_db_unavailable_fails_closed`. Added
`test_market_quality.check_error_fails_closed_not_open`.

## Commands Run / Test Results

See `PLAN1_COMMAND_LOG.md`. All green:
`test_fail_closed_gates` 24/24 · `test_p7_wire` 7/7 · `test_p8_wire` 8/8 ·
`test_bankroll` 12/12 · `test_market_quality` 12/12 · `test_acceptance` 18/18 ·
`python run_tests.py --no-llm` → **55/55 modules passed** (exit 0). No skips.
pytest not used (stdlib runner is the supported path). No live/DB-mutating commands run.

## Remaining fail-open review (Phase 12)

| Search term | Remaining in gate code | Safe? | Explanation |
| ----------- | ---------------------: | ----- | ----------- |
| old bare reasons (`ev_gate_error`, `can_trade_error`, …) | 0 live | yes | replaced by `*_fail_closed`; only test docstrings mention "fail-open" as history |
| `return True` (bankroll.py) | 2 | yes | `can_trade` healthy pass + `exposure_ok` under-cap allow — both AFTER a successful read/eval |
| `return True` (market_quality.py) | 3 | yes | the non-blocked path of each check_*; the ERROR paths now return False |
| `return True` (predict_today.py) | 8 | yes | MiroFish gate (Plan 4, out of scope) + evidence gate + `coerce`-driven wrappers + 2 post-`open_position` success returns |
| `"allow": True` | 0 | yes | risk_guards.evaluate computes `allow=len(failed)==0`; its except now returns `allow=False` |
| `except Exception` (gate paths) | several | yes | every one in EV/risk/bankroll/exposure/market-quality now returns a BLOCK, never an allow |

## Proof — how each gate behaves now

| Condition | EV | Risk | Bankroll | Exposure |
| --------- | -- | ---- | -------- | -------- |
| module missing | BLOCK `ev_gate_unavailable_fail_closed` | BLOCK `risk_guards_unavailable_fail_closed` | BLOCK `bankroll_unavailable_fail_closed` | BLOCK `exposure_unavailable_fail_closed` |
| exception thrown | BLOCK `ev_gate_error_fail_closed` | BLOCK `risk_guards_error_fail_closed` (+ `risk_guards_internal_error_fail_closed` at the core) | BLOCK `bankroll_error_fail_closed` | BLOCK `exposure_error_fail_closed` |
| DB locked/unavailable | n/a (pure math) | BLOCK (positions read fails inside evaluate → internal-error block) | BLOCK `bankroll_unavailable_fail_closed` (get_state raises) | BLOCK `exposure_invalid_fail_closed` (no bankroll baseline) |
| invalid / malformed data | BLOCK `ev_gate_invalid_fail_closed` | BLOCK `risk_guards_invalid_fail_closed` | BLOCK `bankroll_invalid_fail_closed` | BLOCK `exposure_invalid_fail_closed` |
| normal PASS | ALLOW `positive_ev_after_costs` | ALLOW (`ok`) | ALLOW (`ok`) | ALLOW (`ok`) |
| normal BLOCK | `neg_ev_after_costs` | `stale_price`/`high_spread`/… | `drawdown_pause`/`loss_limit`/`cooldown` | `theme_exposure_cap`/`event_exposure_cap` |

And criterion #9 (no bet on failure) is proven two ways: (a) `_skip`/`_sd_skip`
never call `wallet.open_position` and return False after recording the no-bet
(runtime test); (b) a structural scan asserts every gate result is consumed by an
`if not <ok>:` branch (which `return _skip(...)` / `continue`s) before any
`wallet.open_position` in both files.

## Remaining Risks (Plan 1 only)

- **Exposure position-undercount edge**: `portfolio_guards.open_positions()` returns
  `[]` on a read error (used broadly; left unchanged). If the *positions* table were
  unreadable while the *wallet* table was fine, `exposure_ok` could undercount open
  stake. This is covered upstream: `can_trade` runs first on every bet path and its
  `wallet.get_state()` reads BOTH tables, so a dead positions table makes `can_trade`
  block before exposure is reached (defense in depth).
- `opinion_loss_streak()` still degrades to 0 on a mid-read error (analytics helper;
  only relaxes the cooldown, never approves a bet, and runs after the DB readability
  precondition has already passed).
- Out of scope (later plans, untouched): swarm minimum-agent safety, `strategy_bet`
  bypass, wallet atomicity, sameday evidence parity, event portfolio, parser,
  MiroFish, CLV/Gate-2.

---

## Final verification (Plan-1 close-out)

Branch: `fix/fail-closed-money-gates` (committed as 8ea3920).

### Exact files changed

Modified (9, tracked):
```
 M harness/bankroll.py
 M harness/market_quality.py
 M harness/predict_today.py
 M harness/risk_guards.py
 M harness/sameday.py
 M harness/tests/test_bankroll.py
 M harness/tests/test_market_quality.py
 M harness/tests/test_p7_wire.py
 M harness/tests/test_p8_wire.py
```
New (4, untracked — Plan 1):
```
?? harness/safety_gate.py
?? harness/tests/test_fail_closed_gates.py
?? PLAN1_FAIL_CLOSED_REPORT.md
?? PLAN1_COMMAND_LOG.md
```
NOT part of Plan 1 (pre-existing ruflo artifacts — leave uncommitted): `agentdb.rvf`, `agentdb.rvf.lock`.

`git diff --stat` (modified tracked files): 9 files, +245 / -163.

### Tests run — PASS/FAIL

| Command | Result |
| ------- | ------ |
| `python run_tests.py --no-llm` | **55/55 modules passed** (exit 0, no FAIL) |
| `python -m harness.tests.test_fail_closed_gates` | 24/24 (exit 0) |
| `python -m harness.tests.test_p7_wire` | 7/7 (exit 0) |
| `python -m harness.tests.test_p8_wire` | 8/8 (exit 0) |
| `python -m harness.tests.test_bankroll` | 12/12 (exit 0) |
| `python -m harness.tests.test_market_quality` | 12/12 (exit 0) |
| `python -m harness.tests.test_acceptance` | 18/18 (exit 0) |

No skips. pytest not used (the stdlib `run_tests.py` is the supported runner). No
live-service / live-DB / trading commands were run.

### Confirmation — the four money gates now FAIL CLOSED

- **EV** (`_p7_ev_gate`): module-missing → `ev_gate_unavailable_fail_closed`;
  exception → `ev_gate_error_fail_closed`; bad price/prob/side or malformed result
  → `ev_gate_invalid_fail_closed`. Healthy +EV still ALLOWS.
- **Risk** (`_p8_risk_guards` + `risk_guards.evaluate` + `market_quality.check_*`):
  module-missing → `risk_guards_unavailable_fail_closed`; exception →
  `risk_guards_error_fail_closed`; malformed verdict → `risk_guards_invalid_fail_closed`;
  internal error in evaluate → `risk_guards_internal_error_fail_closed`; a quality
  check that errors → `market_quality_error_fail_closed`. Only an explicit
  `allow is True` passes.
- **Bankroll** (`_p9_can_trade` + `bankroll.can_trade`): module-missing/unreadable/
  uninitialized wallet → `bankroll_unavailable_fail_closed`; exception →
  `bankroll_error_fail_closed`; malformed → `bankroll_invalid_fail_closed`. Dead/
  locked DB now BLOCKS (was silently `(True,"ok")`).
- **Exposure** (`_p9_exposure_ok` + `bankroll.exposure_ok`): module/theme-tagger
  missing → `exposure_unavailable_fail_closed`; exception → `exposure_error_fail_closed`;
  no positive bankroll baseline / bad input → `exposure_invalid_fail_closed`.

`wallet.open_position` is never reached on a fail-closed skip — proven by the
runtime `_skip`/`_sd_skip` tests (record no-bet, return False, never open) and the
structural scan asserting every gate call site is consumed by `if not <ok>:` →
skip/continue before any `wallet.open_position`, in BOTH predict_today.py and sameday.py.

---

(Sections below are filled in as Plan 1 progresses: Fixed Gates, New Block Reasons,
Tests Added, Commands Run, Test Results, Remaining Risks, Proof No Safety Gate Fails Open.)
