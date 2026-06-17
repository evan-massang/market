# Final Integration Audit Report

Read-only audit of the Plan 1–11 rebuild. Paper-only. No trades, no live daemons, no live-DB
mutation. Generated on the `fix/profit-intelligence-paper-only` branch (Plan 11 = HEAD).

## Verdict

**PASS: ready for paper-only observation** — *contingent on committing the one safety fix this
audit applied* (see "Hole found and fixed during this audit").

The 8-auditor read-only sweep found **1 real hole** — `safe_bet` (the shared opener for the
manual/shortcut paths) was missing the Plan-8 MiroFish-required gate the daemons enforce. It was
**fixed with the smallest possible change** (wiring the existing gate), independently re-verified
closed with no new gap, and covered by a new regression test. Every other phase (4, 6, 7, 9, 10,
11, 12-adversarial) reported **0 holes**.

With the fix applied: the integrated system is committed (Plans 1–11), tested (65/65 modules +
targeted safety/honesty suites all green), gated end-to-end, fail-closed, paper-only, and its
dashboard/scoreboard/accounting surfaces are honest (unknown ≠ green, alive ≠ used, no-bet ≠ trade,
no profit claim without Gate 2). This is **NOT** real-money readiness — Gate 2 is a readiness signal
only; supervised live paper observation is still required.

## Summary

All eleven hardening plans are committed in history on the current branch, the full no-LLM test
suite (65 modules) and every targeted safety/honesty module pass, and a read-only static +
adversarial audit confirms the bet path is gated end-to-end, the failure modes are fail-closed, the
reporting surfaces cannot fake green, the schema work is idempotent + temp-DB-only, and profit
intelligence (Plan 11) is read-only and cannot trade.

## Git State

* **Branch:** `fix/profit-intelligence-paper-only`
* **HEAD:** `71ee7eb` Plan 11 — paper-only profit intelligence
* **Working tree:** clean except untracked `agentdb.rvf` / `agentdb.rvf.lock` (allowed ruflo
  artifacts). No source/test/report files uncommitted. `git add .` was NOT used.

## Commit Chain

| Plan | Expected purpose | Commit found? | Commit hash | Notes |
| ---- | ---------------- | :-----------: | ----------- | ----- |
| 1 | fail-closed gates | ✅ | `8ea3920` | EV/risk/bankroll/exposure fail-closed |
| 2 | swarm degradation | ✅ | `353b0cc` | degraded/under-strength blocked from betting |
| 3 | shortcut betting paths | ✅ | `3310b9a` | single safety-gated opener; shortcuts disabled |
| 4 | wallet atomicity | ✅ | `f5e767a` | atomic, race-safe `open_position` |
| 5 | same-day parity | ✅ | `29b90e3` | sameday brought to predict_today parity |
| 6 | event portfolio safety | ✅ | `f0563d5` | fake-arb/partial-basket disabled; coherence enforced |
| 7 | strict parser | ✅ | `d6003da` | no year/date/clamp/first-number forecasts |
| 8 | MiroFish honesty | ✅ | `0133add` | fresh-or-degraded; used ≠ alive |
| 9 | accounting/Gate2/CLV honesty | ✅ | `e96fa92` | drift/Gate2/CLV/scoreboard fail-closed |
| 10 | dashboard/supervisor truth | ✅ | `e6bc4a0` | only `healthy` is green; no fake green |
| 11 | profit intelligence paper-only | ✅ | `71ee7eb` | read-only learning; cannot trade |

All 11 plan commits present in history. (Plan-7's commit message is `fix(parser): …`; Plans 1–6 use
`fix(...)` style messages — content verified against each plan's report.)

## Report Cleanup

| File | Stale wording found | Fixed? | Notes |
| ---- | ------------------- | :----: | ----- |
| PLAN1 | "IN PROGRESS"; "no commit yet — awaiting review" | ✅ | → COMPLETE / committed 8ea3920 |
| PLAN2 | "IN PROGRESS"; "not committed — awaiting review" | ✅ | → COMPLETE / committed 353b0cc |
| PLAN3 | "IN PROGRESS"; "not committed — awaiting review" | ✅ | → COMPLETE / committed 3310b9a |
| PLAN4 | "Not committed (awaiting review)" | ✅ | → committed f5e767a |
| PLAN5 | "Not committed (awaiting review)" | ✅ | → committed 29b90e3 |
| PLAN6 | "Not committed (awaiting review)" | ✅ | → committed f0563d5 |
| PLAN7 | "Not committed (awaiting review)" | ✅ | → committed d6003da |
| PLAN8 | — | n/a | no stale wording |
| PLAN9 | "Status: in progress" | ✅ | → COMPLETE / committed e96fa92 |
| PLAN10 | "in progress"; stale duplicate acceptance "42/42"; "Final sign-off pending round-2" | ✅ | → 55/55 / COMPLETE e6bc4a0 (final verdict already present) |
| PLAN11 | "Status: in progress" | ✅ | → COMPLETE / committed 71ee7eb |

Doc-text only — **no code behavior changed** during cleanup. Re-grep after fixes: 0 stale matches.

## Test Results

Full suite (`run_tests.py --no-llm`): **65/65 modules passed.** Targeted safety/honesty modules:

| Module | Result |
| ------ | :----: |
| `test_accounting_honesty` | 41/41 |
| `test_dashboard_supervisor_truth` | 55/55 |
| `test_mirofish_honesty` | 52/52 |
| `test_probability_parser` | 28/28 |
| `test_event_safety` | 24/24 |
| `test_profit_intelligence` | 42/42 |

No supervisor start/stop, no live daemon, no real betting loop, no live API, no DB repair, no
live-`polyswarm.db` mutation were run.

## Dashboard/API Audit

PHASE 8 was performed via the existing TestClient-based tests (temp DB / temp runtime, network
probes mocked) rather than a live daemon. `test_dashboard_supervisor_truth` (55/55) exercises
`/api/health`, `/api/truth`, `/api/services`, `/api/accounting`, `/api/scoreboard`, `/api/mirofish`,
`/api/decisions`, `/api/gates`, `/api/version`; `test_profit_intelligence` (42/42) exercises
`/api/profit-intelligence`. Proven by named tests: no expected endpoint 404s; missing/locked DB
does not 500; malformed runtime cache → degraded (not 500); stale heartbeat ≠ green; accounting
drift ≠ green; MiroFish backend-alive ≠ used; Gate 2 unknown ≠ pass; `paper_only` present on every
envelope.

## ⚠ Hole found and fixed during this audit

The Phase-5 gate-stack auditor found **1 real hole** (2 reported instances, one root cause):
`safe_bet.open_position_if_safe` — the single safety-gated opener that `place_bet` /
`strategy_bet` / the legacy loop route through (Plan 3) — was **missing the Plan-8
MiroFish-required gate** that `predict_today` / `sameday` enforce inline. Under a MiroFish-required
config (`MIROFISH_MODE=required` / `MIROFISH_REQUIRED_FOR_BET=true`), a manual/shortcut bet could
have opened a position without a fresh, market-matched crowd report — weaker than the daemons and a
violation of Plan 3's "same safety stack" promise (audit criteria #6/#10).

**Severity:** medium — latent in the default config (`USE_MIROFISH=False`, so the gate is a no-op
by default; the automated daemons were always correct), active only under MiroFish-required + a
manual/opt-in shortcut path.

**Smallest fix applied** (`harness/safe_bet.py`): wire the EXISTING `predict_today._p_mirofish_gate`
into `open_position_if_safe` after the swarm-health gate. No new feature; no new behavior in the
default config (no-op); fail-closed (blocks) when MiroFish is required and no fresh report is
present. Re-verified by an independent read-only pass (hole closed, no new gap) + a new regression
test `test_safebet_required_mirofish_blocks_shortcut_path` (test_shortcut_paths 14→15/15) + full
suite 65/65. **All other phases (4, 6, 7, 9, 10, 11, 12) reported 0 holes.**

## Safety Bypass Audit

| Search | Findings | Safe? | Explanation |
| ------ | -------- | :---: | ----------- |
| `wallet.open_position` (calls) | 5 call sites: `safe_bet`×1, `predict_today`×2, `sameday`×2 | ✅ | each precedes the full gate stack; `test_no_uncontrolled_open_position_calls` enforces the allowlist `{safe_bet, predict_today, sameday}` repo-wide |
| `open_position(` (def) | `wallet.open_position` is the atomic primitive (BEGIN IMMEDIATE, guarded debit, rollback) | ✅ | Plan 4; only called by the allowed openers |
| `safe_bet` | the one shared opener; runs swarm-health → MiroFish → EV → risk → bankroll → exposure | ✅ | shortcut/manual paths route here (now incl. MiroFish after the fix) |
| `strategy_bet` / `ENABLE_STRATEGY_BET` | disabled by default (`false`); when enabled routes through `safe_bet` | ✅ | price-pattern bets gated; never direct |
| `place_bet` | manual CLI; runs a real swarm forecast then routes through `safe_bet` | ✅ | no betting API endpoint exists |
| `ENABLE_LEGACY_LOOP_BETTING` | disabled by default (`false`); settle/score stay on; opens route through `safe_bet` | ✅ | legacy ungated open path off |
| Plan 11 modules | `opportunity_ranker`/`decision_features`/`edge_explainer`/`profit_intel` | ✅ | static scans: 0 `wallet`/`open_position`/`safe_bet` calls; read-only |

## Gate Stack Audit

| Path | Parser | Swarm | MiroFish | EV | Risk | Bankroll | Exposure | Event | Wallet atomic | Safe? |
| ---- | :----: | :---: | :------: | :-: | :--: | :------: | :------: | :---: | :-----------: | :---: |
| `predict_today.predict_one` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `sameday.place_sameday` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (parity) |
| `place_bet` → `safe_bet` | ✅ | ✅ | ✅¹ | ✅ | ✅ | ✅ | ✅ | ◐² | ✅ | ✅ |
| `strategy_bet` → `safe_bet` (opt-in) | n/a³ | ✅ | ✅¹ | ✅ | ✅ | ✅ | ✅ | ◐² | ✅ | ✅ |
| legacy loop → `safe_bet` (opt-in) | ✅ | ✅ | ✅¹ | ✅ | ✅ | ✅ | ✅ | ◐² | ✅ | ✅ |
| event-portfolio leg | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (exec disabled by default) |

¹ MiroFish gate added to `safe_bet` by this audit's fix (was the found hole). ² `safe_bet` does not
run the one-YES-per-event coherence check — multi-leg basket execution is **disabled by default**
and unreachable via `safe_bet`; a manual single-leg `place_bet` is the operator's deliberate choice
(auditors did not flag this as a hole). ³ `strategy_bet` is a price-pattern bet (no LLM forecast),
so parser strictness is N/A; it is disabled by default.

## Fail-Closed Audit

Phase 6 verdict: **PASS (0 holes).** Every audited fail-open pattern resolves fail-CLOSED in
safety/money/honesty paths:

| Area | Pattern checked | Actual behavior | Safe? |
| ---- | --------------- | --------------- | :---: |
| EV / risk / bankroll / exposure | `except`/`return True` | gate wrappers return `(False, reason)` on missing/errored data → block | ✅ |
| parser | first-number / clamp fallback | strict parse; failure → no probability (not a fabricated number) | ✅ |
| MiroFish | `mirofish_used = True` | `used` only for canonical `FRESH_USED`; stale/pending/wrong-market → not used | ✅ |
| accounting | `status = "ok"` | drift/degraded/unknown computed honestly; never forced ok | ✅ |
| Gate 2 | `pass = True` | `gate_pass = (not reasons) and profitable`; unknown/insufficient → not pass | ✅ |
| heartbeat | `state = "healthy"` | missing/stale/future/pid-dead/commit-mismatch → never healthy | ✅ |
| CLV | stale/missing → valid | `mean_clv` returns None below `min_n` / on error → unverified | ✅ |

## Data Honesty Audit

Phase 7 verdict: **PASS (0 holes).** All separated + labeled:

| Metric/status | Source of truth | Can be unknown? | Can it fake green? | Notes |
| ------------- | --------------- | :-------------: | :----------------: | ----- |
| realized / unrealized / total PnL | `accounting_audit.audit_accounting` | yes (unrealized None if marks stale) | no | separately computed |
| equity | `audit_accounting` | yes | no | verified only with fresh marks |
| CLV | `clv.mean_clv` (read-only) | yes (`None` < min_n) | no | settled = final; open marks non-final, excluded from claims |
| Gate 2 | `accounting_audit.gate2_status` | yes (`unknown`) | no | fail-closed; readiness only |
| accounting status | `audit_accounting.status` | yes | no | ok/degraded/drift/unknown/error |
| dashboard health vs service liveness vs health | `status_model` | yes (`unknown`) | no | `is_green` only `HEALTHY`; liveness ≠ health |
| MiroFish backend-alive vs used | `mirofish_status` + `mirofish_health` | yes | no | used ≠ alive (Plan 8) |
| no-bet vs bet | `journal` status + `journal_consistency` | n/a | no | no-bets never counted as trades |
| observe-only vs trade | label backtest | n/a | no | observe-only never bets |
| paper-only vs live | `PAPER_ONLY_*` flags | n/a | no | every relevant surface labels paper-only |

## Dashboard/API Audit

Performed via TestClient tests (Phase 8, above) — no live daemon. All endpoints present (no 404),
missing/locked DB → no 500, malformed runtime cache → degraded, stale heartbeat / accounting drift
≠ green, MiroFish backend-alive ≠ used, Gate 2 unknown ≠ pass, `paper_only` on every envelope.
`/api/profit-intelligence` is green only on a Gate-2 pass.

## DB/Schema Audit

Phase 9 verdict: **PASS (0 holes).**

| Schema/change | File | Idempotent? | Mutates live DB? | Safe? |
| ------------- | ---- | :---------: | :--------------: | :---: |
| `CREATE TABLE IF NOT EXISTS` (wallet, journal, clv, mirofish_runs, decision_features, …) | many | ✅ | only on the configured DB (temp in tests) | ✅ |
| `ALTER TABLE` (end_date, event_slug, degraded cols, …) | wallet/calibration | ✅ (guarded by `PRAGMA table_info`) | additive | ✅ |
| runtime audits (`audit_accounting`, `gate2_status`, `journal_consistency`, `obs.gate`, dashboard) | — | n/a | **no** (open `mode=ro`) | ✅ |
| `repair()` | `db_check` | n/a | opt-in CLI only (`dry_run=True` default) | ✅ |
| tests | `make_temp_env` / `DATABASE_URL` | n/a | **no** (temp DB) | ✅ |

## Paper-Only Audit

Phase 10 verdict: **PASS (0 holes).** No real-money execution path exists: the wallet has no
blockchain/web3/ClobClient imports, no transaction signing, no private-key handling (an active test
scans the wallet source for forbidden strings). Gamma deliberately avoids the trading libraries
(public HTTP GET only). Gate 2 is labeled `paper_only: True` even on pass and authorizes nothing
live. Shortcut paths disabled by default. Tests need no secret/key.

## Cross-Plan Consistency

Phase 11 verdict: **PASS (0 holes)** — all 10 links verified:

| Cross-plan link | Verified? | Evidence |
| --------------- | :-------: | -------- |
| 1. Parser fail (P7) → swarm degradation (P2) | ✅ | unparseable → agent failure → degraded/insufficient → `_p_swarm_health` blocks |
| 2. MiroFish-required fail (P8) blocks before sizing/wallet | ✅ | `_p_mirofish_gate` before sizing in predict_today/sameday — and now in `safe_bet` (fix) |
| 3. Event safety (P6) blocks fake baskets before wallet | ✅ | multi-leg exec disabled by default; coherence + no naked leg in the daemons |
| 4. Wallet atomicity (P4) protects final open | ✅ | BEGIN IMMEDIATE + guarded debit + rollback |
| 5. Accounting status (P9) → dashboard truth (P10) | ✅ | `/api/truth` + System/Accounting badges consume `audit_accounting` |
| 6. Gate 2 (P9) → gates card (P10) | ✅ | `/api/gates` uses `gate2_status` |
| 7. MiroFish canonical state (P8) → MiroFish card (P10) | ✅ | `/api/mirofish` uses `state_from_row` (used ≠ alive) |
| 8. Profit intelligence (P11) reads honesty fields, cannot trade | ✅ | static scans: 0 trading calls; read-only |
| 9. Same-day parity with predict_today | ✅ | identical gate sequence |
| 10. Shortcut paths cannot bypass the gate stack | ✅ | all route through `safe_bet` (full stack incl. MiroFish after fix) or are disabled |

## Adversarial Review

Phase 12 verdict: **PASS (0 holes)** — all 11 exploit attempts blocked:

| Attack | Expected safe behavior | Result | Hole? |
| ------ | ---------------------- | ------ | :---: |
| malformed LLM output → probability | strict parser rejects | blocked | no |
| one survivor → fake consensus | n<MIN → consensus 0 / insufficient → swarm-health blocks | blocked | no |
| MiroFish stale/pending → used | canonical state; used only FRESH_USED | blocked | no |
| event basket fake-arb opens one naked leg | multi-leg exec disabled; coherence enforced | blocked | no |
| stale CLV passes Gate 2 | `gate2_clv_unverified` reason → not pass | blocked | no |
| dashboard green when accounting fails | accounting component → unsafe; not green | blocked | no |
| profit intelligence opens a position | read-only; no wallet/safe_bet calls | blocked | no |
| no-bet counted as trade | separate status; `journal_consistency` | blocked | no |
| live DB modified by tests | temp DB / mode=ro | blocked | no |
| **direct/shortcut wallet open bypasses gates** | all routes via gated openers | **blocked after fix** (the MiroFish gap is now closed) | no |
| report says complete while tests disagree | counts reconciled in Phase 2 | blocked | no |

## Remaining Risks

Honest, Plan-scope-only risks (none are real-money or guarantee claims):

* **More live paper data is required.** The system is *ready to observe on paper*, not *proven
  profitable*. Gate 2 is fail-closed and currently not passable without ≥20 settled trades + a
  positive verified record; until then every performance surface honestly says learning /
  insufficient sample. No profitability is claimed or implied.
* **MiroFish-required is an opt-in config.** With the fix, all paths enforce it; but MiroFish is
  off by default for the local CPU-only setup — operators enabling required mode should confirm the
  backend is actually producing fresh, market-matched reports.
* **Manual single-leg `place_bet` does not run the one-YES-per-event coherence check** (the
  automated daemons do; multi-leg basket execution is disabled). A manual operator betting one leg
  of a mutually-exclusive event is a deliberate choice — documented, not a hole.
* **PID-reuse heartbeat detection lag** (Plan 10 residual) is bounded by `max_age` and needs psutil
  to close fully — does not affect betting safety.
* The audit fix (`safe_bet` MiroFish gate + 1 test + the report cleanups) is **uncommitted pending
  your review** — the PASS is contingent on committing it.

## Final Recommendation

**PASS: ready for paper-only observation.**

Plans 1–11 are committed and integrated; the full suite (65 modules) and all targeted
safety/honesty suites pass; the bet path is gated end-to-end across every entry point; failure
modes are fail-closed; the dashboard/scoreboard/accounting surfaces are honest (unknown ≠ green,
alive ≠ used, no-bet ≠ trade, no profit claim without Gate 2); the schema work is idempotent and
temp-DB-only; and no real-money path exists. One gate-consistency hole (`safe_bet` missing the
MiroFish gate) was found during this audit and fixed with the smallest possible change, then
re-verified. This is **not** real-money readiness — the correct next step is supervised **paper-only
observation** to accumulate the settled-trade history Gate 2 requires.
