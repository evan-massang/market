# Plan 9 — DB Drift, Gate 2, CLV, Scoreboard Honesty Report

**Branch:** `fix/scoreboard-accounting-honesty` · **Mode:** PAPER-ONLY (no trades, no live daemons,
no DB repair, no live `polyswarm.db` mutation).

> Status: in progress. Phase 1 inspection table below; implementation + tests + verification follow.

## Phase 1 — Inspection table

Schema: `paper_wallet(id, starting_bankroll, cash, realized_pnl, created_at, updated_at)`;
`paper_positions(id, market_id, question, side, model_p, market_p, edge, stake, fill_price,
shares, fee, status['open'|'settled'|'closed'], outcome, payout, realized_pnl, end_date,
opened_at, settled_at, event_slug)`; `decisions(id, ts, market_id, question, model_p, market_p,
edge, side, stake, fill_price, regime, signal, status['bet'|'no_bet'], why)`;
`clv_records(id, market_id, side, entry_price, closing_price, clv, theme, recorded_at)`;
`clv_snapshots(... bucket, entry_price, snap_price, clv, opened_at, recorded_at)`.

| Area | File/function | Current behavior | Honesty risk | Fix |
| ---- | ------------- | ---------------- | ------------ | --- |
| cash | `wallet.get_state` | from `paper_wallet.cash`; guarded debit can't go negative (Plan 4) | low (cannot go negative) | audit reads + asserts `cash >= 0` |
| open positions | `wallet.get_open_positions` | listed; Plan 4 no-dup + Plan 6 event coherence enforced at open | dup/multi-YES only enforced at write, not audited at read | audit re-checks dup-open + multi-YES-per-event |
| realized PnL | `wallet.get_state/settle_market` | `realized_pnl` from settled/closed only; idempotent | **labeled/used as if it were total PnL** (Gate 2 reads realized-only) | audit separates realized vs total; Gate 2 uses verified equity |
| unrealized PnL | — | **does not exist** anywhere | **open underwater positions never reflected** | audit computes `unrealized = open_mark_value - open_cost_basis` via marks |
| equity | `wallet.get_state` | `equity = cash + open_exposure` **AT COST** | **at-cost equity overstates true MTM equity for underwater books** | audit computes MTM equity via `bankroll.mark_to_market_equity`; unverified if marks missing/stale |
| settled positions | `wallet.settle_market` | idempotent, sets payout/realized | low | audit reconciles realized vs ledger |
| stale open positions | `paper_positions.end_date` | open rows kept indefinitely; no auto-settle | **expired-but-unsettled invisible** | audit flags `unsettled_expired` (end_date < now) |
| CLV | `clv._clv_for`, `loop.py:489` record | formula **is side-aware** (YES c-e, NO e-c) BUT settlement records `closing_price=market_p` (**entry price, frozen**) | **CLV sign/magnitude wrong at settlement; no mark timestamp; no final/open flag** | add `compute_clv(...)` (side+timestamp+final aware); gate CLV honesty |
| Gate 2 | `scoreboard.compute:152`, `obs/gate.evaluate:269` | `n_settled>=30 and realized_pnl>0 and equity(at-cost)>=start`; **obs copy omits min-N** | **passes on at-cost equity, no accounting/drift/baseline/CLV/segmentation/drawdown; two inconsistent copies** | unify into fail-closed `gate2_status()` w/ all required-metric reasons |
| dashboard health | `dashboard.api_health`/`/health`, `health.snapshot` | green = service liveness + table freshness only | **green while wallet↔ledger drifted / equity unverified** | health card consumes accounting audit; distinguish alive/fresh/verified/profitable |
| scoreboard rows | `scoreboard.compute/render` | no freshness ts; dict lacks paper flag; no unrealized/total split; thin-sample unlabeled | **stale looks current; realized shown as total; ROI w/o equity** | add freshness, `accounting_status`, `paper_only`, realized/unrealized/total/equity, sample labels |
| decision journal | `journal.record_decision`, dashboard `bets` count | `status` 'bet' vs 'no_bet'; bets count filters `status='bet'` | **already honest** (no-bet ≠ trade) | add read-only consistency checks (orphan trade/position, no-bet-as-trade) |
| paper wallet integrity | `db_check.run/ledger_reconciliation_report/repair` | read-only `mode=ro`; OK/WARN/FAIL; **repair opt-in (`--repair`), never implicit** | drift visible only in db_check, not surfaced to gate/dashboard | fold accounting audit into db_check; add UNKNOWN; surface to gate/scoreboard/dashboard |

## Summary

**What changed.** A single read-only accounting truth model (`harness/accounting_audit.py`) now
underpins every performance/health surface, so the bot can never show fake equity, fake CLV, a
fake Gate-2 pass, or a green dashboard while the book is stale/inconsistent/unverifiable.

**Why.** Equity was reported at-cost (open positions never marked to market); "total PnL" was
realized-only; CLV at settlement used the entry market price; Gate 2 passed on realized-only,
at-cost numbers with no accounting/drift/baseline/CLV/segmentation/drawdown checks (and two
inconsistent copies); nothing surfaced data freshness or accounting status.

**Files changed.**
```
NEW  harness/accounting_audit.py                 audit_accounting() + gate2_status() + journal_consistency()
NEW  harness/tests/test_accounting_honesty.py    41 tests
EDIT harness/clv.py                              + compute_clv() (side/timestamp/final-aware)
EDIT harness/scoreboard.py                       Gate 2 -> gate2_status(); accounting/freshness/paper_only; honest render
EDIT harness/obs/gate.py                         Gate 2 unified -> gate2_status() (read-only)
EDIT harness/db_check.py                         + accounting audit summary, unsettled_expired, UNKNOWN
EDIT harness/health.py                           snapshot() + accounting{status,verified,...} + paper_only
EDIT harness/dashboard.py                        /api/state + accounting/paper_only; NEW /api/accounting
EDIT harness/tests/test_scoreboard.py            ready-fixture for the strict Gate 2
```

## Old Dangerous Behavior

* **DB drift could be hidden** — `db_check` detected wallet↔ledger drift but the gate, scoreboard
  and dashboard never consumed it; a drifted realized_pnl (the number Gate 2 reads) flowed
  straight into "PASS".
* **Equity / PnL could be misleading** — `equity = cash + open_stake_at_cost`. An open position
  down 50% still counted at full cost, so equity (and "bankroll grew") overstated reality. "Total
  PnL" was realized-only; unrealized PnL did not exist anywhere.
* **CLV could be wrong** — at settlement, CLV was computed with `closing_price = market_p`, the
  market price stored AT ENTRY. The sign and magnitude could be entirely wrong (e.g. -0.01 instead
  of +0.48); no mark timestamp, no final-vs-open distinction.
* **Gate 2 could pass on stale/partial metrics** — realized-only, at-cost equity, no accounting
  audit, no min-sample in the obs copy, no baseline, no segmentation, no CLV validity, no drawdown.
* **Dashboard could look green while accounting was unverified** — the health badge keyed off
  service liveness + table freshness only; a healthy daemon with a drifted wallet showed green.

## New Behavior

* **Accounting audit** (`audit_accounting`) — one READ-ONLY (`mode=ro`) truth model: cash, open
  cost basis, open mark value (via a mark source), realized/unrealized/total PnL, MTM equity,
  wallet↔ledger drift, stale-mark count, and detections for negative cash / invalid rows /
  duplicate opens (Plan 4) / multiple-YES-per-event (Plan 6) / stale marks / unsettled-expired.
  Never writes, never repairs; `ok=True` only when status=="ok".
* **Invariant checks** — equity == cash + open_mark_value; total_pnl == equity − starting; missing
  or stale marks ⇒ equity/total are `None` (UNVERIFIED), status `degraded`; drift/negative/invalid
  ⇒ status `drift` (blocking); DB unavailable ⇒ `error`; cannot compute ⇒ `unknown`.
* **CLV formula** — `compute_clv()` is side-aware (YES: mark−entry, NO: entry−mark), validates
  entry∈(0,1) and mark∈[0,1], and is timestamp-aware: an OPEN market needs a FRESH mark
  (`clv_final=false`); a SETTLED market uses the final close (`clv_final=true`, no staleness
  check). Missing/stale/invalid ⇒ a reason and `ok=False`, never a fake number.
* **Gate 2 fail-closed** (`gate2_status`) — passes ONLY when accounting is verified (no drift,
  equity MTM-verified), with ≥30 settled trades over ≥3 days, a baseline comparison, valid CLV,
  a theme/environment segmentation, within-limit drawdown, no single outlier dominating PnL, a
  reported uncertainty (95% CI), and a genuinely profitable verified equity. Any missing/stale/
  unverifiable metric ⇒ a specific `gate2_*` reason and non-pass. Now unified across
  `scoreboard.compute` and `obs/gate.evaluate`.
* **Scoreboard fields** — `accounting{status,reasons,cash,realized/unrealized/total,equity,drift,
  mark_stale_count,open/settled_count}`, `generated_at`, `paper_only`; render shows
  "unverified" instead of a fake number and prints Gate-2 reasons.
* **Dashboard honesty** — `health.snapshot()` adds `accounting{status,verified,drift,...}` +
  `paper_only`; `/api/state` adds the accounting block + `equity_verified`; NEW `/api/accounting`
  returns audit + gate2 + journal. Service-alive / data-fresh / accounting-verified are distinct.
* **Journal consistency** (`journal_consistency`) — read-only checks: no-bet not counted as a bet,
  no-bet/rejected market that became a trade (FAIL), trade without a decision, bet without a
  position, missing timestamp / market_id.

## Accounting Truth Model

```
starting_bankroll        = paper_wallet.starting_bankroll
cash                     = paper_wallet.cash                       (>= 0, else drift)
open_cost_basis          = sum(stake)            over OPEN
open_mark_value          = sum(shares * side_price(mark)) over OPEN  (needs FRESH marks)
realized_pnl             = sum(realized_pnl)     over SETTLED/CLOSED   (the LEDGER truth)
unrealized_pnl           = open_mark_value - open_cost_basis           (None if unverified)
equity                   = cash + open_mark_value                      (None if unverified)
total_pnl                = equity - starting_bankroll                  (None if unverified)
equity_from_wallet_state = cash + open_cost_basis                      (the old at-cost number)
ledger_cash              = starting - sum(stake) - sum(fee) + sum(payout over closed)
drift                    = max(|cash - ledger_cash|, |wallet.realized - ledger_realized|)
```
Assumptions: binary $1 payout per share; YES side_price = mark, NO side_price = 1 − mark; a mark
with no verifiable timestamp is treated as STALE (never trusted); equity is verifiable only when
there are no open positions OR every open position has a fresh mark.

## Gate 2 Requirements (all must hold to PASS)

1. accounting audit status == ok (no drift, verified equity)  · 2. no wallet↔ledger drift ·
3. ≥ 30 settled/closed trades · 4. ≥ 3 days calendar coverage · 5. equity MTM-verified (no
stale/missing marks) · 6. CLV computed from valid marks (≥5 records) · 7. baseline comparison
exists · 8. results segmented by theme/environment · 9. uncertainty (95% CI) reported · 10. max
drawdown ≤ 25% of starting · 11. no single trade > 50% of total |PnL| · 12. profitable on
verified equity (realized>0 AND equity≥start) · 13. paper_only explicit. Missing ⇒ fail/unknown.

## CLV Definition

```
YES bet:  CLV = mark_or_close - entry          (positive = price rose after we bought)
NO  bet:  CLV = entry - mark_or_close          (positive = price fell after we bought)
open market   -> mark must be FRESH (mark_time within max_age) ; clv_final=false
settled market-> use the final close/settlement price          ; clv_final=true
missing/stale/invalid mark or entry/side -> reason + ok=False (never a number)
```

## New Reasons / Statuses

* **accounting status:** ok · degraded · drift · unknown · error
* **accounting reasons:** accounting_ok · accounting_db_unavailable · accounting_missing_table ·
  accounting_negative_cash · accounting_invalid_position · accounting_duplicate_open_position ·
  accounting_multiple_yes_same_event · accounting_mark_price_missing · accounting_mark_price_stale ·
  accounting_equity_unknown · accounting_equity_drift · accounting_realized_pnl_mismatch ·
  accounting_unsettled_expired_position
* **CLV reasons:** clv_ok · clv_mark_missing · clv_mark_stale · clv_invalid_entry_price ·
  clv_invalid_side · clv_invalid_mark_price · clv_unknown
* **Gate-2 status:** pass · fail · unknown.  **reasons:** gate2_accounting_unverified ·
  gate2_db_drift · gate2_insufficient_sample · gate2_insufficient_time · gate2_clv_unverified ·
  gate2_no_baseline · gate2_unsegmented_results · gate2_outlier_dominated · gate2_drawdown_exceeded ·
  gate2_not_profitable · gate2_pass
* **journal checks:** no_bet_not_a_bet · no_bet_counted_as_trade · trade_without_decision ·
  bet_without_position · decision_timestamp · decision_market_id
* **db_check:** PASS / WARN / FAIL / UNKNOWN

## Tests Added

`harness/tests/test_accounting_honesty.py` — **41/41**: accounting audit (clean equity, negative
cash, invalid/duplicate/multi-YES, missing-mark, stale-mark, db-unavailable, unsettled-expired,
no-repair), CLV (YES/NO, stale, missing, invalid side, final price, open mark, not-PnL), Gate 2
(fail on unverified/drift/sample/baseline/stale-CLV; uncertainty reported; pass only when all
valid), scoreboard/dashboard (PnL split, paper-only, stale→degraded, no fake equity, health
not-green on drift, unknown-not-fake, /api/accounting), journal (no-bet≠trade, rejection≠trade,
no-bet-as-trade detected, orphan trade, bet-without-position), and 4 static scans (Gate-2 needs
accounting ok, CLV side-aware, dashboard depends on audit, repair opt-in only).

## Commands Run

See `PLAN9_COMMAND_LOG.md`.

## Test Results

* `test_accounting_honesty` → **41/41 passed**
* `test_scoreboard` → **8/8**, `test_gate_readonly` → **1/1** (gate stays byte-for-byte read-only)
* full suite `run_tests.py --no-llm` → **63/63 modules passed** (62 prior + 1 new). No regressions.

## Remaining Risks (Plan 9 scope only)

* The accounting audit's mark-to-market needs a caller-supplied mark source; with no live price
  feed the audit reports `degraded` (equity UNVERIFIED) — correct fail-closed behavior, but a
  live dashboard/Gate-2 wanting verified equity must wire a fresh price source.
* `loop.py` still RECORDS settlement CLV from `market_p` (entry price) as a documented proxy; the
  honest `compute_clv()` exists and the gate/analytics no longer trust a stale settlement CLV, but
  wiring loop.py to capture a real pre-resolution close price is a follow-up (out of this plan's
  measurement-honesty surface — it is a data-capture change in the live loop).
* Time-coverage / drawdown / outlier thresholds are conservative defaults; tune per policy.

## Proof

* **Accounting drift is detected** — `test_negative_cash_fails`, `gate2_fails_on_db_drift`,
  `dashboard_health_not_green_when_accounting_fails` (drift → status `drift`, gate FAIL, badge
  unverified).
* **Stale marks degrade equity/CLV** — `stale_mark_makes_equity_unverified` (equity None),
  `clv_stale_mark_unknown` (clv None).
* **Gate 2 cannot pass when metrics unverified** — fails on unverified/drift/sample/baseline/
  stale-CLV; passes ONLY when all valid (`gate2_pass_only_when_all_valid`).
* **No-bets are not counted as trades** — `no_bet_not_counted_as_trade`,
  `wallet_rejection_not_counted_as_trade`, `no_bet_with_position_is_detected` (FAIL when violated).
* **Dashboard cannot show green when accounting fails** — health `verified=False` on drift/unmarked.
* **DB repair is opt-in only** — `db_check.repair(dry_run=True)` default; `accounting_audit.py` has
  zero UPDATE/INSERT/DELETE.

## Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Accounting audit exists, read-only by default | ✅ `accounting_audit.audit_accounting`, `mode=ro`, 0 writes |
| 2 | Negative cash / invalid / duplicate / incoherent detected | ✅ tests 2–5 |
| 3 | Equity separates cash / open mark / realized / unrealized / total | ✅ truth model + `scoreboard_separates_pnl` |
| 4 | Missing/stale marks ⇒ equity/CLV unknown, not fake | ✅ tests 6,7,13,14,28,29 |
| 5 | CLV side-aware and timestamp-aware | ✅ `compute_clv` + tests 11–17, static scan |
| 6 | Gate 2 fails closed on drift / missing metrics | ✅ tests 19–23 |
| 7 | Gate 2 requires sample, baseline, valid CLV (or unknown) | ✅ tests 21–23,25 |
| 8 | Scoreboard labels paper-only + freshness | ✅ `scoreboard_labels_paper_only`, `generated_at` |
| 9 | Dashboard cannot show green when accounting unverified | ✅ tests 30,31 |
| 10 | No-bets / rejected / observe-only not counted as trades | ✅ tests 33–35 + journal_consistency |
| 11 | Tests prove the above | ✅ 41/41 |
| 12 | Existing tests still pass | ✅ 63/63 modules |
| 13 | Report written | ✅ this file |

**Verdict:** ✅ **PLAN 9 COMPLETE.** The bot is proven unable to fake equity, fake CLV, fake a
Gate-2 pass, or show green dashboard health when accounting / marks / results are incomplete,
stale, drifted, or unverifiable. Paper-only throughout; no live DB mutation; DB repair opt-in only.
