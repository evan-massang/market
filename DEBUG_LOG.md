# DEBUG LOG

Every issue found during the audit: command/where, symptom, root cause, file
responsible, fix. Severity: 🔴 critical (crash / corruption / lying to user) ·
🟠 major (wrong behavior) · 🟡 minor (UX / staleness / fragility).

Status: OPEN → FIXED (commit) / WONTFIX (rationale) / NOT-A-BUG (verified).

---

## Phase 1 — reproduction findings (ground truth)

### D1 🟡 `predict_today once --dry-run` runs a real 235s LLM forecast (flag ignored)
- **Where:** `python -m harness.predict_today once --max 1 --dry-run`
- **Symptom:** expected a fast stub forecast; instead ran a full swarm forecast
  (235s on CPU) + challenger ensemble, then decided NO BET (correlated_exposure).
- **Root cause:** `--dry-run` is a `loop`/`LoopConfig` concept; `predict_today`'s
  arg parser silently accepts unknown flags and never sets `cfg.dry_run`, so the
  real swarm runs. Silent flag-swallowing hides user mistakes.
- **File:** `harness/predict_today.py` (arg parsing in the `once`/`daemon` entry).
- **Fix needed:** either honor `--dry-run` (stub path) in `predict_today`, or
  reject unknown flags with a clear message. (to confirm in Phase 4/8 audit)
- **Status:** OPEN

### D2 🟡 Stray duplicate test files in `harness/` root
- **Where:** `harness/test_challenger.py`, `test_classifier.py`, `test_scoreboard.py`,
  `test_sizing.py`, `test_wallet.py` (canonical tests live in `harness/tests/`).
- **Symptom:** two copies of "the wallet test", etc. Risk of editing the wrong one
  / stale assertions / confusion about which is authoritative.
- **Root cause:** legacy test files predating the `harness/tests/` package.
- **Fix needed:** confirm whether `run_tests.py` discovers them; if stale, remove
  or fold into `harness/tests/`. (to confirm in Phase 0/3 audit)
- **Status:** OPEN

### D3 🟡 `predict_today` FIND stage is slow with no progress output
- **Where:** `predict_today once` — ~60-235s with the terminal showing only the
  stage header, no incremental feedback during the live multi-window Gamma scan.
- **Root cause:** swarm forecast is ~235s/market on CPU (known) + the scan fetches
  several windows serially before any output.
- **Fix needed:** progress feedback / bounded scan; not a correctness bug but a
  "looks hung" fragility. (tuning, low priority)
- **Status:** OPEN

### Verified WORKING (not bugs)
- `harness.doctor` ✅ (11 PASS/1 WARN/0 FAIL, never raised a stack trace).
- `run_tests.py` ✅ 41/41 modules; defaults to no-LLM (only `--llm` opts in) — the
  documented `run_tests.py` does not hang on LLM.
- `harness.scoreboard`, `harness.loop status`, `harness.obs.gate`, `harness.metrics`
  ✅ all run read-only and report honestly (gates FAIL, not faked).
- Full P0-P13 pipeline ✅ ran end-to-end against the live system (forecast →
  challenger ensemble → experiment tag → P8 correlated_exposure guard → NO BET),
  confirming the new code works in production, not just in tests.

---

## Phase 2-17 — deep code audit findings

A 14-subsystem adversarial audit (each finding independently reproduced before fix)
surfaced **41 confirmed defects + 2 refuted**. Fixed in priority batches; each fix
ships a regression test. Severity counts: 1 critical, 18 major, 22 minor.

### BATCH 1 — data integrity / "don't lie about P&L" — FIXED

- **A1 🔴 CRITICAL — settlement double-credit (Gate-2 corruption).** `wallet.settle_market`
  / `close_at_price` guarded `status='open'` only on the SELECT; the per-row UPDATE and
  the wallet credit had no status guard and no cross-process lock, so two daemons
  (sameday + predict_today on the same `polyswarm.db`) could credit the same position
  twice — silently inflating `realized_pnl`, the number Gate 2 reads. Reproduced
  (cash 1019→1058 on re-settle). **FIX:** guarded `UPDATE … WHERE id=? AND status='open'`
  + `rowcount==1` check before crediting the wallet (idempotent — a row transitions and
  credits exactly once), plus `busy_timeout=30s` in `_conn()` so a concurrent daemon
  waits instead of erroring. `harness/wallet.py`. Test: `test_settlement_idempotent` 6/6
  (settle-twice, true two-connection race, close-then-settle, close-twice). FIXED.
- **A2 🟡 close_at_price fee + open affordability.** `close_at_price` omitted the fee
  (overstating closed P&L vs `settle_market`); `open_position` checked affordability
  against `stake` but debited `stake+fee` (negative cash possible). Dormant at the default
  `fee_frac=0.0` but now consistent. **FIX:** `realized = sell_value - stake - fee`;
  affordability vs `stake*(1+fee_frac)`. Tests included above. FIXED.
- **A3 🟠 wallet↔ledger drift unreconciled (Gate 2 trusts a possibly-wrong number) +
  missing `db_check`.** **FIX:** new `harness/db_check.py` (`python -m harness.db_check`,
  read-only): PRAGMA integrity_check, table presence, **reconciles wallet cash/realized/
  equity against the positions ledger**, flags negative cash / bad status / settled-vs-
  closed split / unresolved-but-bet forecasts. On the LIVE db it immediately surfaces the
  real drift (wallet realized −42.25 vs ledger −39.43; equity invariant off $40.44) —
  honest + debuggable. Test: `test_db_check` 4/4 (clean reconciles OK, drift WARNs,
  missing table FAILs, run is read-only). FIXED.

Suite: 41 → 43 modules, all green.

_(remaining batches appended below as each is closed)_
