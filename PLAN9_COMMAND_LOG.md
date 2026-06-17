# Plan 9 — Command Log

Every command for Plan 9 (DB drift / Gate 2 / CLV / scoreboard accounting honesty). Paper-only.
Branch: `fix/scoreboard-accounting-honesty`.

## Phase 0 — prep

```
(Get-Location).Path             -> C:\Users\OMEN\Pictures\Polymarket (session root); repo is .\polyswarm
git rev-parse --abbrev-ref HEAD -> (was fix/mirofish-freshness-honesty)
git status --short              -> clean except ?? agentdb.rvf / agentdb.rvf.lock (ruflo artifacts)
git log --oneline -12           -> 0133add Plan 8 … 8ea3920 Plan 1 (ALL committed)
git checkout -b fix/scoreboard-accounting-honesty -> Switched to a new branch
```
Plans 1-8 committed (0133add = Plan 8). agentdb.rvf* stay untracked. Safe to proceed.

## Phase 1 — inspection (Glob/Grep/Read + 5 parallel read-only Explore agents)

Mapped: wallet/settlement, scoreboard/metrics/profitability, Gate 2 (scoreboard.compute +
obs/gate.evaluate), CLV (clv.py + loop.py settlement record), db_check, dashboard health,
journal. Inspection table in the report. Key findings: equity is at-cost (no MTM); CLV at
settlement uses `market_p` (entry price); Gate 2 realized-only/at-cost with two inconsistent
copies (obs copy omits min-N); no freshness/accounting status on scoreboard/dashboard.

## Phases 2-9 — implementation (files written/edited)

```
NEW  harness/accounting_audit.py   audit_accounting() (truth model, read-only) +
                                   gate2_status() (fail-closed Gate 2) + journal_consistency()
EDIT harness/clv.py                + compute_clv() (side+timestamp+final aware) + _to_epoch()
EDIT harness/scoreboard.py         Gate 2 -> gate2_status(); + accounting/generated_at/paper_only;
                                   render shows verified/unverified, never fake-green
EDIT harness/obs/gate.py           Gate 2 unified -> gate2_status() (read-only); +gate2_status/reasons
EDIT harness/db_check.py           + accounting_audit summary + unsettled_expired + UNKNOWN status
EDIT harness/health.py             snapshot() + accounting{status,verified,drift,...} + paper_only
EDIT harness/dashboard.py          /api/state + accounting/paper_only/equity_verified; NEW /api/accounting
NEW  harness/tests/test_accounting_honesty.py   41 tests
EDIT harness/tests/test_scoreboard.py           _rebuild -> genuinely Gate-2-READY fixture
```

## Phases 10-11 — tests + regression

```powershell
$env:PYTHONUTF8 = "1"   # console is cp1252; reports print unicode
.\.venv\Scripts\python.exe -m harness.tests.test_accounting_honesty   -> 41/41 passed
.\.venv\Scripts\python.exe -m harness.tests.test_scoreboard           -> 8/8 passed
.\.venv\Scripts\python.exe -m harness.obs.tests.test_gate_readonly    -> 1/1 (gate stays read-only)
.\.venv\Scripts\python.exe run_tests.py --no-llm                      -> 63/63 modules passed
```
(62 prior + new test_accounting_honesty. One mid-build fix: gate2_status must read CLV via a
mode=ro query, never `clv.mean_clv()` which CREATEs clv_records — else the gate evaluator would
write. Fixed; test_gate_readonly green.)

## Phase 12 — static verification (Grep)

```
accounting_audit.py  UPDATE/INSERT/DELETE  -> 0 matches (READ-ONLY, no repair)
gate2_status reasons -> gate2_db_drift, gate2_accounting_unverified, gate2_clv_unverified,
                        gate2_no_baseline, gate2_insufficient_sample, ... , gate2_pass
clv.py side-aware    -> clv.py:82 `return c - e` (YES) ; clv.py:84 `return e - c` (NO)
db_check.py          -> def repair(dry_run: bool = True)  (opt-in; never implicit)
health.py            -> snapshot() reads audit_accounting() -> accounting{verified,...}
tests                -> temp DB only (make_temp_env); no live polyswarm.db mutation
```
NOTE: db_check / scoreboard / gate were NOT run against the live polyswarm.db (read-only by
construction, but skipped to honor "do not touch live DB"). Coverage is via temp-DB tests.
