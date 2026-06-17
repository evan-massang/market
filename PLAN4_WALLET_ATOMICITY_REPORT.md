# Plan 4 — Wallet Atomicity Report

> Goal: `wallet.open_position` must be atomic and race-safe — never negative cash,
> never exposure-cap bypass, never a duplicate-open race, never a partial write.
>
> Paper-only. No real-money execution. Branch: `fix/wallet-open-position-atomic`.
> Built on Plans 1-3 (8ea3920, 353b0cc, 3310b9a).

Status: **COMPLETE** — 58/58 test modules pass (incl. the new 17-case Plan-4 module).
Committed as f5e767a.

## Current behavior map (Phase 1)

| Function | File | Reads cash? | Checks exposure? | Debits cash? | Inserts position? | Same transaction? | Race risk? |
| -------- | ---- | ----------: | ---------------: | -----------: | ----------------: | ----------------: | ---------: |
| open_position **(OLD)** | wallet.py | yes — `_cash()` own conn | yes — `get_open_exposure()` 2nd conn | yes — 3rd conn, UNGUARDED | yes — 3rd conn | **NO** (3 conns) | **HIGH** — TOCTOU; 2 daemons both pass checks then both debit |
| open_position **(NEW)** | wallet.py | yes — inside txn | yes — inside txn | yes — GUARDED `WHERE cash>=?` + rowcount | yes — inside txn | **YES** — one `BEGIN IMMEDIATE` txn | **NONE** — writers serialized |
| settle_market | wallet.py | n/a | n/a | credits | updates status | yes (`_conn`+commit) | low — already guarded `status='open'`+rowcount (prior audit) |
| close_at_price | wallet.py | n/a | n/a | credits | updates status | yes | low — already guarded (prior audit) |
| safe_bet / predict_today / sameday | (callers) | — | — | via open_position | via open_position | — | inherit open_position's atomicity; all check `fr.opened` |

## Summary

`wallet.open_position` performed its cash read, exposure read, and the
debit+insert across **three separate unprotected connections**, with an
**unguarded** cash UPDATE and **no duplicate-open protection**. Two daemons
(sameday + predict_today) could both read the same cash, both pass the
affordability/exposure checks, and both debit — overdrawing cash, bypassing the
exposure cap, or opening the same market twice. Plan 4 rewrites `open_position` to
do everything inside **one `BEGIN IMMEDIATE` transaction** with a **guarded,
rowcount-checked** cash debit and a duplicate-open block, rolling the whole thing
back on any failure.

## Old Dangerous Behavior

* **Cash/exposure read and debit were not atomic** — `_cash()`, `get_open_exposure()`,
  and the INSERT+UPDATE each opened their own connection. A concurrent caller could
  interleave between the check and the debit.
* **Concurrent daemons could overdraw / bypass exposure** — both read cash=X, both
  passed `X >= stake`, both debited → negative cash; same for the exposure cap.
* **Unguarded debit** — `UPDATE paper_wallet SET cash = cash - ?` had no `WHERE cash >= ?`
  and no rowcount check, so it would debit into the negative.
* **Duplicate opens** — nothing stopped two opens on the same `market_id`.
* **Partial-write risk** — INSERT then UPDATE with no explicit rollback coordination.

## New Behavior

* **Transaction model:** one `sqlite3` connection, `isolation_level=None` (manual
  control), `PRAGMA busy_timeout=30000`, then `BEGIN IMMEDIATE`. All of: read cash,
  read open exposure, affordability/per-bet/exposure/duplicate checks, the cash
  debit, and the position INSERT happen **inside that one transaction**, ending in a
  single `COMMIT` (or `ROLLBACK`).
* **`BEGIN IMMEDIATE` lock strategy:** takes the write (RESERVED) lock up front, so a
  second concurrent `open_position` BLOCKS at its own `BEGIN IMMEDIATE` (waiting on
  busy_timeout) until the first COMMITs — then re-reads the **already-debited** cash
  and is rejected. Writers are serialized; there is no interleave window.
* **Guarded cash update:** `UPDATE paper_wallet SET cash = cash - ? WHERE id=1 AND
  cash >= ?` + `if cur.rowcount != 1: ROLLBACK` → the debit can only succeed if cash
  still covers it, and it can never drive cash negative.
* **Rollback:** any `OperationalError` (lock) → `ROLLBACK` + `wallet_db_locked_or_unavailable`;
  any other exception (e.g. the INSERT failing after the debit) → `ROLLBACK` +
  `wallet_insert_failed_rolled_back` → cash unchanged, no position row.
* **Duplicate-open policy:** by default, at most ONE open position per `market_id`
  (`SELECT 1 ... WHERE market_id=? AND status='open'` under the lock → block with
  `wallet_duplicate_open_blocked`). A caller may pass `allow_duplicate=True` to opt
  out (none currently do). **No DB unique index was added** — existing historical
  rows may contain legitimate duplicates and an index migration could fail; the rule
  is enforced at runtime under the transaction lock instead (see Remaining Risks).
* **Caller handling:** `FillResult` shape is unchanged (`opened`/`reason`/…), so
  `safe_bet`, `predict_today`, and `sameday` already branch on `fr.opened` and record
  a wallet-rejected no-bet; only the `reason` strings became canonical `wallet_*` codes.

## Files Changed

| File | Purpose |
| ---- | ------- |
| `harness/wallet.py` | `open_position` rewritten: one `BEGIN IMMEDIATE` txn, busy_timeout, in-txn reads, guarded+rowcount-checked debit, duplicate block, rollback, canonical reasons, `allow_duplicate` kwarg. (settle/close unchanged.) |
| `harness/db_check.py` | New read-only checks: `duplicate_open_positions`, `open_stake_positive`, `open_price_valid`. |
| `harness/tests/test_wallet.py` | `test_guardrails` updated to assert canonical `wallet_*` reasons. |
| `harness/tests/test_wallet_atomic.py` (NEW) | 17 cases incl. 4 thread-concurrency races + rollback fault injection. |

## New Wallet Rejection Reasons

`wallet_invalid_side`, `wallet_invalid_price`, `wallet_invalid_stake`,
`wallet_insufficient_cash`, `wallet_per_bet_cap_exceeded`,
`wallet_exposure_cap_exceeded`, `wallet_duplicate_open_blocked`,
`wallet_atomic_update_failed`, `wallet_db_locked_or_unavailable`,
`wallet_insert_failed_rolled_back`, `wallet_uninitialized`.
(Success reason unchanged: `filled`.)

## Concurrency Protection

Two simultaneous `open_position` calls each create their own connection and issue
`BEGIN IMMEDIATE`. SQLite grants the RESERVED write lock to exactly one; the other
blocks (up to `busy_timeout=30s`, in practice microseconds) until the first COMMITs.
The winner reads cash, passes its checks, debits via the guarded
`WHERE cash >= ?`, inserts, COMMITs. The loser then proceeds, reads the **updated**
cash/exposure/open-positions, and is rejected by the affordability check, the
exposure cap, or the duplicate check — never debiting. Result: at most one of two
racing opens succeeds; cash is never negative; the exposure cap is never bypassed;
a market is never double-opened. Proven by `test_concurrent_opens_cannot_overdraw_cash`,
`..._cannot_bypass_exposure_cap`, `..._cannot_duplicate_same_market`, and
`..._different_markets_both_succeed`.

## Tests Added

`harness/tests/test_wallet_atomic.py` (17): success_debits_and_inserts;
insufficient_cash/exposure_cap/invalid_side/invalid_price/invalid_stake block (cash
unchanged); duplicate_open_blocked_by_default; duplicate_open_allowed_when_explicit;
insert_failure_rolls_back_debit (fault-injected INSERT → cash restored, no row);
db_locked_returns_failure_no_partial_write; guarded_update_is_present (structural);
concurrent_opens_cannot_overdraw_cash; ..._cannot_bypass_exposure_cap;
..._cannot_duplicate_same_market; ..._different_markets_both_succeed;
safe_bet_handles_wallet_rejection_as_no_bet; predict_today_and_sameday_check_fr_opened.
Plus `test_wallet.test_guardrails` updated.

## Commands Run / Test Results

See `PLAN4_COMMAND_LOG.md`. `test_wallet_atomic` 17/17 (exit 0);
`python run_tests.py --no-llm` → **58/58 modules passed** (exit 0, no FAIL, no skips
beyond the pre-existing LLM-integration self-skip). pytest not used. No live-service /
live-DB / trading commands run.

## Static verification (Phase 10)

| Search | Result | Safe? | Explanation |
| ------ | ------ | ----- | ----------- |
| `BEGIN IMMEDIATE` | wallet.py:217 | yes | open_position runs in one immediate-locked txn |
| `busy_timeout` | wallet.py:215 (in txn) | yes | concurrent writer waits for the lock, doesn't error |
| `UPDATE paper_wallet ... WHERE id=1 AND cash >= ?` | wallet.py:261 | yes | guarded debit — cannot go negative |
| `cur.rowcount != 1` | wallet.py:263 | yes | debit success verified; rollback otherwise |
| `wallet_duplicate_open_blocked` | wallet.py:256 | yes | duplicate open blocked by default |
| `wallet_atomic_update_failed` | wallet.py:265 | yes | guarded-debit failure → rollback, no insert |
| `wallet.open_position(` callers | safe_bet(1), predict_today(2), sameday(2) | yes | all check `fr.opened`; tests are temp-DB only |

## Remaining Risks (Plan 4 only)

* **No DB unique index for open positions.** The duplicate-open rule is enforced at
  runtime inside the `BEGIN IMMEDIATE` transaction (a `SELECT` under the write lock),
  NOT by a partial unique index. This was deliberate: a live DB may already hold
  legitimate historical duplicate opens (e.g. event-portfolio legs predate this), and
  a `CREATE UNIQUE INDEX` migration would fail on them. Runtime enforcement is
  race-safe because it runs under the same write lock as the debit/insert. Adding a
  partial unique index after a one-time de-dup is possible future work.
* **`allow_duplicate=True` bypasses the duplicate block** (no caller uses it today).
  It exists for a future event-portfolio multi-leg-per-market need; it does NOT bypass
  cash/exposure/per-bet checks.
* settle_market / close_at_price were left unchanged (already idempotency-guarded by a
  prior audit; out of Plan-4 scope, which is `open_position`).

## Proof

* **Insufficient cash cannot debit** — `test_insufficient_cash_blocks_cash_unchanged`
  (reason `wallet_insufficient_cash`, cash unchanged, no row).
* **Exposure cap cannot be bypassed** — `test_exposure_cap_blocks_cash_unchanged` +
  `test_concurrent_opens_cannot_bypass_exposure_cap`.
* **Duplicate open cannot happen by default** — `test_duplicate_open_blocked_by_default`
  + `test_concurrent_opens_cannot_duplicate_same_market`.
* **Insert failure rolls back cash** — `test_insert_failure_rolls_back_debit`
  (fault-injected INSERT → cash restored, zero positions).
* **Concurrent opens cannot overdraw** — `test_concurrent_opens_cannot_overdraw_cash`
  (two threads, room for one; exactly one opens, final cash 0, never negative).
* **Callers don't treat rejection as success** — `test_safe_bet_handles_wallet_rejection_as_no_bet`
  + `test_predict_today_and_sameday_check_fr_opened`; full suite 58/58 (predict_today /
  sameday / safe_bet caller tests green).

## Phase 12 — acceptance criteria

1. open_position is atomic — YES (one BEGIN IMMEDIATE txn).
2. cash check + debit in same transaction — YES.
3. exposure check + insert in same transaction — YES.
4. debit has rowcount/success verification — YES (`cur.rowcount != 1` → rollback).
5. insert failure rolls back debit — YES (`test_insert_failure_rolls_back_debit`).
6. DB lock/unavailable cannot create a partial position — YES (`test_db_locked_..._no_partial_write`).
7. cash cannot go negative from a race — YES (`test_concurrent_opens_cannot_overdraw_cash`).
8. exposure cap cannot be bypassed from a race — YES (`..._cannot_bypass_exposure_cap`).
9. duplicate open blocked by default / explicitly controlled — YES (`allow_duplicate` default false).
10. caller handles wallet rejection as no-bet — YES (safe_bet + predict_today/sameday).
11. concurrency tests prove race safety — YES (4 thread races).
12. existing tests still pass — YES (58/58).
13. report written — YES (this file).
