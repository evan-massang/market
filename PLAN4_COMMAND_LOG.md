# Plan 4 — Command Log

Every command for Plan 4 (wallet atomicity + race conditions). Paper-only.
Branch: `fix/wallet-open-position-atomic`.

## Phase 0 — prep

```
(Get-Location).Path             -> C:\Users\OMEN\Pictures\Polymarket\polyswarm
git rev-parse --abbrev-ref HEAD -> (was fix/gate-shortcut-betting-paths)
git status --short              -> clean except ?? agentdb.rvf / agentdb.rvf.lock (ruflo artifacts)
git log --oneline -5            -> 3310b9a Plan3, 353b0cc Plan2, 8ea3920 Plan1  (ALL committed)
git checkout -b fix/wallet-open-position-atomic  -> Switched to a new branch
```
Plans 1-3 committed -> safe to proceed. agentdb.rvf* untouched.

## Phase 1 — inspect wallet + callers (Grep/Read, read-only)

Read harness/wallet.py (open_position + settle/close), safe_bet.py, predict_today.py,
sameday.py, strategy_bet.py, loop.py, db_check.py, and the wallet-touching tests
(test_wallet, test_settlement, test_settlement_idempotent, test_db_check, test_p7_wire).

OLD open_position race: cash via _cash() (own conn) + exposure via get_open_exposure()
(2nd conn) + INSERT/UPDATE (3rd conn) — checks & debit across THREE unprotected
connections; unguarded `UPDATE paper_wallet SET cash = cash - ?` (no rowcount, no
`cash >= ?`); NO duplicate-open protection. Two daemons can both pass the cash/exposure
checks then both debit -> negative cash / exposure bypass / duplicate open.

Compat scan: NO existing test opens the same market_id twice while OPEN (each resets +
opens once then settles) -> the new duplicate block is safe. Only test_wallet.test_guardrails
asserted reason SUBSTRINGS -> updated to canonical wallet_* codes.

## Phases 2-8 — implementation + tests (no shell)

Rewrote harness/wallet.open_position: one conn, PRAGMA busy_timeout=30000, BEGIN IMMEDIATE,
read cash+exposure IN the txn, validate side/price/stake, affordability/per-bet/exposure/
duplicate checks IN the txn, GUARDED debit (`WHERE id=1 AND cash >= ?` + rowcount==1),
INSERT in same txn, COMMIT; ROLLBACK on any exception; canonical wallet_* reasons +
allow_duplicate kwarg. Added open-position integrity checks to db_check (duplicate_open,
open_stake_positive, open_price_valid). Updated test_wallet.test_guardrails reasons.
NEW harness/tests/test_wallet_atomic.py (17 cases incl. 4 thread-concurrency races).

## Phases 9-10 — test + static verification

```
# interpreter: .\.venv\Scripts\python.exe
python -m harness.tests.test_wallet_atomic   -> 17/17 passed (exit 0)
python -m harness.tests.test_wallet          -> (in full run) passed
python -m harness.tests.test_db_check        -> (in full run) passed
python run_tests.py --no-llm                 -> SUMMARY: 58/58 modules passed (exit 0, no FAIL)
```
pytest not used (run_tests.py is the supported stdlib runner). NOT run (per constraints):
supervisor start/stop, live daemons, real betting loop, db_check --repair, live polyswarm.db.

Static (Grep, read-only) — harness/wallet.py:
```
BEGIN IMMEDIATE                       -> present (line 217)
PRAGMA busy_timeout=30000             -> present (line 215, inside open_position txn)
UPDATE paper_wallet ... WHERE id=1 AND cash >= ?  -> present (line 261, GUARDED debit)
cur.rowcount != 1                     -> present (line 263, debit rowcount check)
wallet_duplicate_open_blocked         -> present (line 256)
wallet_atomic_update_failed           -> present (line 265)
allow_duplicate                       -> present (line 157, default False)

wallet.open_position( callers (non-test): safe_bet.py(1), predict_today.py(2), sameday.py(2)
  — all check fr.opened; none assume success.
```
