# Plan 11 — Command Log

Every command for Plan 11 (profit intelligence, PAPER-ONLY — never weakens Plans 1–10).
Branch: `fix/profit-intelligence-paper-only`.

## Phase 0 — prep

```
(Get-Location).Path             -> C:\Users\OMEN\Pictures\Polymarket (session root); repo is .\polyswarm
git rev-parse --abbrev-ref HEAD -> (was fix/dashboard-supervisor-truth)
git status --short              -> clean except ?? agentdb.rvf / agentdb.rvf.lock (ruflo artifacts)
git log --oneline -6            -> e6bc4a0 Plan 10 … (Plans 1-10 ALL committed)
git checkout -b fix/profit-intelligence-paper-only -> Switched to a new branch (from e6bc4a0)
```
Plans 1-10 committed (e6bc4a0 = Plan 10). agentdb.rvf* stay untracked. Safe to proceed.

## Phase 1 — inspection (Glob/Grep/Read + 6 parallel read-only Explore agents)

Mapped predict_today/sameday decision flow, journal schema, scoreboard/adaptive/metrics/clv/
accounting_audit signatures, market_quality/risk_guards/portfolio_guards, dashboard/command_center.
Key facts: `decisions` table has NO json column (→ additive `decision_features` table, no
migration); candidate dicts carry `_label/_hl/_price/_rank_score/_subscores/_exit_risk/_stale/
_theme/_observe_only/liquidity/volume/outcome_prices/event_slug`; decision locals at bet/no-bet
sites: `final_p/p/bp/meta.consensus/pack.evidence_quality/sz.edge/conv`; `_skip()` is the canonical
no-bet recorder. Surface map in the report.

## Phases 2-9 — implementation (files written/edited)

```
NEW  harness/opportunity_ranker.py  rank_candidates() pre-forecast ranking (buckets+reasons+blocked);
                                    PAPER_ONLY_PROFIT_INTELLIGENCE; reads ONLY a pre-forecast whitelist
NEW  harness/decision_features.py   additive decision_features table + build_snapshot/record/get
NEW  harness/edge_explainer.py      explain_edge() (after-cost EV, no-bet-as-signal, BANNED_PHRASES)
NEW  harness/profit_intel.py        summarize_no_bets / summarize_post_trade_learning / attribution /
                                    profit_intelligence_report; insufficient-sample/unverified guards
EDIT harness/dashboard.py           NEW /api/profit-intelligence (Plan 10 envelope; green ONLY on gate2 pass)
EDIT harness/predict_today.py       _skip + _log_decision_features at 3 inline decision sites (best-effort)
EDIT harness/sameday.py             _sd_skip + _log_sd_features at 2 bet sites (best-effort)
NEW  harness/tests/test_profit_intelligence.py   40 tests
```

## Phases 10-11 — tests + regression

```powershell
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m harness.tests.test_profit_intelligence  -> 40/40 passed
.\.venv\Scripts\python.exe run_tests.py --no-llm                      -> 65/65 modules passed
```
(temp DB only; mirofish index patched in one test; NO live APIs/daemons/DB; NO paper bets placed.)

## Phase 12 — static verification (Grep)

```
opportunity_ranker.py  wallet.open_position / .open_position( / safe_bet( / future-field .get()  -> 0
profit_intel.py        PAPER_ONLY_PROFIT_INTELLIGENCE / paper_only                               -> 8
dashboard /api/profit-intelligence  state="ok" ONLY when gate2_pass (else learning/degraded)
```

## Phase 12b — adversarial verification (workflow plan11-adversarial-profit-intel)

8 skeptics — profit-intel-trades / future-data-leak / fake-profit-claim / gate-bypass-via-wiring /
stake-inflation / no-bet-as-failure-or-trade-count / overfit-and-unsafe-language / live-db-or-daemon.

Round 1: 6 CLEAN; 1 genuine fix; 1 false positive (verified):

```
FIX  by_theme_pnl exposed win_rate for tiny samples (n=1 -> 100%). _guard_theme_pnl() withholds
     win_rate/ROI for themes < MIN_SEGMENT_N (post-process; adaptive.theme_pnl untouched so sizing
     is unaffected). +1 test.
FP   gate2 "missing elif for status 'fail'" -> VERIFIED FALSE: audit_accounting returns only
     ok/degraded/drift/unknown/error (lines 295-303), all handled; no 'fail' status exists; the
     no-bet-as-trade case is caught via accounting DRIFT. Plan 11 only consumes gate2 read-only.
```

```powershell
.\.venv\Scripts\python.exe -m harness.tests.test_profit_intelligence  -> 41/41 passed (+1)
.\.venv\Scripts\python.exe run_tests.py --no-llm                      -> 65/65 modules passed
```

Round 2 (re-attack): 7 CLEAN; 1 genuine consistency fix:

```
FIX  guard half-applied: command_center.py / metrics.py / scoreboard.py still called
     adaptive.theme_pnl() raw -> tiny-sample win-rate reached /api/command_center. Added public
     profit_intel.guarded_theme_pnl() and applied at all 3 display sites (display-only; scoreboard
     adaptive_min_edge/sizing still reads raw theme_pnl). +1 test.
```

```powershell
.\.venv\Scripts\python.exe -m harness.tests.test_profit_intelligence  -> 42/42 passed (+1)
.\.venv\Scripts\python.exe run_tests.py --no-llm                      -> 65/65 modules passed
```

Round 3 (re-attack): 7 CLEAN; 1 finding DECLINED (out of scope):

```
DECL paper_metrics() overall hit_rate/roi unguarded -> /api/command_center. DECLINED: pre-existing
     Plan-9 metric; contract pinned by test_metrics.py (hit_rate==0.5 on n=4); always returns n;
     overall aggregate (not a per-strategy "proven" claim); user scoped "don't fix everything".
     Plan 11 guards per-SEGMENT win-rates; aggregate shown raw with n. Noted in Remaining Risks.
```

FINAL: core rules CLEAN across all 3 rounds. Genuine findings were all the sample-size-guard class
(fixed for every Plan 11 surface + per-theme win-rate; pre-existing aggregate declined). 1 round-1
"critical" was a verified false positive. PLAN 11 COMPLETE. 42/42 + 65/65. NOT committed (paused).
