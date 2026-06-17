# Plan 11 — Profit Intelligence Report

**Branch:** `fix/profit-intelligence-paper-only` · **Mode:** PAPER-ONLY (no real trades, no live
daemons, no live `polyswarm.db` mutation, no Plan 1–10 gate bypass/loosening).

> Status: **COMPLETE** — committed as 71ee7eb.

## Phase 1 — Inspection table

Mapped via Grep/Read + a 6-agent read-only Explore workflow over predict_today, sameday, journal,
scoreboard/adaptive/metrics/clv/accounting_audit, market_quality/risk_guards/portfolio_guards,
and dashboard/command_center.

| Area | File/function | Current behavior | Profit-intelligence gap | Fix |
| ---- | ------------- | ---------------- | ----------------------- | --- |
| candidate ranking | `predict_today.find_candidates` / `scanner.rank_candidates` (`_rank_score`, `_subscores`, `_exit_risk`) | candidates carry a composite `_rank_score` + sub-scores, ordered opinion-first | composite is internal/opaque; no explainable bucket, no per-candidate **reasons**, no deterministic-**blocked** signal exposed to analytics | NEW `opportunity_ranker.rank_candidates()` → `{rank_score, rank_bucket, rank_reasons, pre_forecast_only}` reusing ONLY pre-forecast fields |
| edge scoring | `sizing.size_bet` (`sz.edge/side/stake/reason`), `_conviction` | edge + conviction computed at decision; only `why` text persisted | edge not separated raw-vs-after-cost in a reusable explainer; conviction factors not surfaced | `edge_explainer.explain_edge()` shows raw edge **and** after-cost EV + positive/negative/uncertainty factors |
| EV explanation | `_p7_ev_gate(final_p, price, side, m, confidence)` | hard after-cost EV gate; reason string on reject | EV reasoning not exposed as structured explanation for the dashboard | explainer consumes `edge_after_costs`/`ev_reason`; dashboard surfaces it |
| no-bet reason analysis | `_skip()` → `journal.record_decision(...,'no_bet', why)`; reason codes (swarm_*, divergence, consensus, no_data, low_evidence, mirofish_*, observe_only, ev/rg/ex, no-edge, wallet_rejected) | every no-bet logged with a reason string | no aggregation/grouping; no-bets look like "nothing happened", not learning | NEW `profit_intel.summarize_no_bets()` groups by canonical reason bucket + top blockers + repeated markets |
| decision feature logging | `journal.decisions` (ts, market_id, question, model_p, market_p, edge, side, stake, fill_price, regime, signal, status, why) | flat columns; **no JSON/details column** | rich decision context (consensus, evidence_quality, divergence, mirofish state, accounting/gate2) not captured | NEW additive `decision_features` table + `decision_features.record()` (no risky migration of `decisions`) |
| market/regime segmentation | `scoreboard.theme_of`, `meta.regime`, `adaptive.theme_pnl` | per-theme realized PnL exists; regime only echoed to journal | no segmentation surface combining theme/regime/liquidity/spread/source for learning | `profit_intel.attribution()` segments by theme/source/mirofish/event/buckets w/ sample sizes |
| model disagreement | Guard B `|swarm_p − challenger_p|`, Guard C `consensus`, `_decision_probability` weights | divergence/consensus gate bets; weights blend swarm/challenger | disagreement not recorded as a learnable feature or explained | captured in `decision_features` (challenger_probability, swarm_probability, divergence, consensus) + surfaced in explainer |
| MiroFish contribution | Plan 8 `mirofish_status.state_from_row` / `mirofish_used`; `_p_mirofish_gate` | canonical used/contribution honesty (Plan 8) | not segmented in learning/attribution | attribution segments mirofish-used vs not; features record mirofish state — **reuses Plan 8, never relaxes it** |
| CLV / post-trade feedback | Plan 9 `clv.mean_clv/clv_by_theme/edge_decay_report`, `accounting_audit.audit_accounting/gate2_status` | CLV + accounting truth exist | not assembled into a guarded post-trade learning summary | NEW `profit_intel.summarize_post_trade_learning()` w/ insufficient_sample/clv_unverified/accounting_unverified |
| strategy attribution | `adaptive.theme_pnl`, `paper_positions` (no `source` column) | per-theme PnL only; source not stored | can't attribute by source/horizon/quality without overclaiming | `attribution()` uses `decision_features.source` when present, else theme/event/mirofish; sample-size warnings |
| dashboard explanation | Plan 9/10 `/api/accounting`, `/api/scoreboard`, `/api/gates`, `_envelope` | accounting/gate2 surfaced honestly | no single profit-intelligence surface for ranking/no-bet/learning context | NEW `/api/profit-intelligence` (Plan 10 envelope; never "profitable" unless gate2 pass) |

**Headline safety facts that constrain Plan 11:** the `decisions` table has no JSON column (→
additive `decision_features` table, no migration); profit intelligence is **read-only over existing
tables** + a non-trading ranker; gate2/accounting truth (Plan 9) is the only source of any
performance claim; MiroFish honesty (Plan 8) is consumed, never relaxed.

## Summary

**What changed.** Five paper-only profit-intelligence surfaces were added — all read-only or
non-trading: `opportunity_ranker` (explainable pre-forecast candidate ranking), `decision_features`
(an additive snapshot table + recorder wired best-effort into both decision daemons),
`edge_explainer` (honest bet/no-bet explanations), `profit_intel` (no-bet learning + post-trade
learning + attribution + a top-level report), and a dashboard `/api/profit-intelligence` endpoint.

**Why.** The bot already had a composite candidate score, per-theme PnL, CLV, and Gate-2/accounting
truth — but nothing that *explained* ranking, turned no-bets into a learning signal, assembled
post-trade learning with honesty guards, or attributed performance by segment with sample-size
warnings. Plan 11 adds that intelligence layer **without touching a single safety/sizing rule**.

**Files changed.** NEW `opportunity_ranker.py`, `decision_features.py`, `edge_explainer.py`,
`profit_intel.py`, `tests/test_profit_intelligence.py`; EDIT `dashboard.py` (+1 endpoint),
`predict_today.py` / `sameday.py` (best-effort feature snapshots at the decision sinks only).

## Old Behavior

* Candidate ranking was an opaque composite score (`scanner._rank_score`) — no explainable bucket,
  no per-candidate reasons, no surfaced deterministic-blocked signal.
* No-bets were logged with a reason string but never **summarised** — they read like "nothing
  happened" rather than the safety/EV logic doing its job.
* Post-trade learning existed only as scattered pieces (CLV, theme PnL, Gate 2) with no assembled,
  honesty-guarded summary; nothing refused a claim on thin data in one place.
* Strategy attribution was per-theme PnL only — no source/MiroFish/event/bucket segmentation with
  sample-size warnings.
* The dashboard had accounting/Gate-2 truth but no single profit-intelligence context surface.

## New Behavior

* **Candidate ranking** — `opportunity_ranker.rank_candidates()` returns `{rank_score, rank_bucket
  (high/medium/low/blocked), rank_reasons, pre_forecast_only}` using ONLY a pre-forecast whitelist
  (liquidity, spread, volume, freshness, time-to-resolution, market type, exit risk, stale/
  observe-only/event-coherence status, MiroFish *availability*). Missing data lowers confidence;
  deterministic blocks are ranked `blocked`, never promoted.
* **Decision feature snapshots** — `decision_features` (additive table) captures the decision-time
  context (forecast/market price, raw + after-cost edge, consensus, divergence, evidence quality,
  liquidity/spread/volume, MiroFish state/used, accounting/Gate-2 status, action, reason,
  blocked-by-gate, paper_only). Recorded best-effort at both daemons' decision sinks — never
  affects a bet.
* **Edge explanation** — `edge_explainer.explain_edge()` returns positive/negative/uncertainty
  factors, blocked-by, why-no-bet (framed as useful, not failure), why-bet, safety-gates-passed —
  always showing **after-cost EV**, never "guaranteed/free money/safe profit".
* **No-bet intelligence** — `summarize_no_bets()` groups no-bets into canonical reason buckets,
  computes top blockers + repeated markets + failure modes, and offers paper-only suggestions that
  **never loosen a gate** (`loosens_safety_gate: false`).
* **Post-trade learning** — `summarize_post_trade_learning()` reports realized/unrealized/total PnL
  (separated), settled vs open, CLV (final), per-theme PnL/CLV, with **fail-closed flags**:
  `insufficient_sample` (< 20 settled), `accounting_unverified`, `clv_unverified`; the performance
  claim is never "profitable" — at most `gate2_pass_paper` on a real Gate-2 pass.
* **Attribution** — `attribution()` segments settled performance by source / MiroFish-used / event /
  liquidity / spread / confidence buckets; win-rate is shown **only** for segments with ≥ 10
  settled trades, else withheld with a warning.
* **Dashboard** — `/api/profit-intelligence` (Plan 10 envelope) is green (`state="ok"`) **only** on
  a Gate-2 pass; otherwise `learning` / `degraded`. Always `paper_only=true` with accounting + Gate
  2 status.
* **Paper-only guardrails** — every module exports `PAPER_ONLY_PROFIT_INTELLIGENCE = True`; none
  import the wallet's `open_position`/`safe_bet`; the ranker reads only a pre-forecast whitelist.

## Safety Boundaries (Plan 11 cannot bypass Plans 1–10)

* **No trading:** the ranker and all profit-intelligence modules never call `wallet.open_position`,
  `safe_bet`, or any sizer (static scan: 0 occurrences). They only read and explain.
* **No sizing feedback:** no Plan 11 module is imported by the sizing path (`sizing.size_bet`,
  `_conviction`, `adaptive_min_edge` are untouched) — uncertainty cannot be hidden to size bigger.
* **No gate bypass:** the wired `_log_decision_features` / `_log_sd_features` / `record_decision`
  calls are best-effort, fully `try/except`-guarded, run AFTER the decision is journaled, and only
  reference locals guaranteed in scope — they cannot alter control flow or skip a gate.
* **No future-data leakage:** the ranker reads only `PRE_FORECAST_FIELDS`; a candidate carrying
  settlement fields produces an identical rank (proven by `ranking_ignores_future_outcome_fields`).
* **No fake profit:** any performance claim is gated by Plan 9 `gate2_status`; thin/unverified data
  yields `insufficient_sample` / `accounting_unverified` / `learning`, never "profitable".
* **No live DB/daemon:** `decision_features` writes only its own additive table; `profit_intel` is
  read-only; nothing starts a daemon or calls a network API.

## Tests Added

`harness/tests/test_profit_intelligence.py` — **42/42**: ranking (5: quality ordering, stale→
blocked, missing-data≠high, future-field invariance, block-not-overridable), decision features
(5: bet/no-bet recorded, paper_only, MiroFish state, accounting/Gate-2), edge explanation (5:
factor lists, −EV/low-evidence no-bet, no unsafe language, after-cost EV), no-bet intelligence
(5: grouping, top blockers, no-bet≠trade, no gate-loosening, repeated markets), post-trade learning
(5: insufficient-sample/accounting/CLV guards, realized/unrealized/total split, open vs settled),
attribution (5: source sample sizes, MiroFish/event/bucket segmentation, insufficient-sample
warnings), dashboard/report (5: paper_only, accounting/Gate-2 status, not-profitable-without-Gate-2,
missing-DB no crash), static scans (5: no open_position, no safe_bet, no unsafe wording, no future
fields in ranking, paper_only present).

## Commands Run

See `PLAN11_COMMAND_LOG.md`.

## Test Results

* `test_profit_intelligence` → **42/42 passed**
* full suite `run_tests.py --no-llm` → **65/65 modules passed** (64 prior + 1 new). No regressions.

## Remaining Risks (Plan 11 scope only)

* Profit intelligence is **learning scaffolding on paper data** — it does not prove the strategy
  works. Far more live paper data (≥ 20 settled trades, and a Gate-2 pass) is needed before any
  performance number should be trusted; until then every surface says learning / insufficient
  sample / watching.
* Attribution `source` is only as rich as `decision_features.source`; pre-Plan-11 settled trades
  attribute to `unknown` (honestly labelled).
* The decision-feature snapshots are best-effort — under a DB lock a snapshot may be skipped; this
  never affects a bet, but the learning table can have gaps (acceptable for analytics).
* **Pre-existing aggregate metrics (out of Plan 11 scope):** `metrics.paper_metrics()` reports the
  OVERALL book `hit_rate`/`roi`/`profit_factor` for any `n` (always returned WITH `n`), and that
  raw aggregate reaches `/api/command_center`. Plan 11 guards *per-segment/strategy* win-rates
  (theme/source/bucket) but deliberately does NOT modify this Plan-9 metric — its contract is
  pinned by existing tests and the overall stat shows its own sample size. A future dedicated
  "metrics display honesty" pass could add a `sufficient_sample` flag to the aggregate display
  without breaking the metric's contract.

## Proof

* **Ranking does not trade / leak future data:** static scan of `opportunity_ranker.py` →
  0 `open_position`/`safe_bet`/future-field accesses; `ranking_ignores_future_outcome_fields`.
* **Profit intelligence does not open positions:** `static_ranker_never_opens_position`,
  `static_profit_intel_never_bypasses_safe_bet`.
* **No unsafe profit claims:** `no_unsafe_profit_language`, `static_no_unsafe_profit_wording`.
* **No-bets become learning signals:** `no_bets_grouped_by_reason`, `top_blockers_computed`,
  `recommendations_never_loosen_gates`; `total_no_bets` excludes bets.
* **Gate 2 still controls any performance claim:** `report_not_profitable_unless_gate2_pass`,
  `insufficient_sample_blocks_claims`, `accounting_unverified_blocks_claim`.
* **Paper-only is visible:** `report_returns_paper_only`, `static_paper_only_present`, the endpoint
  always carries `paper_only=true`.

## Adversarial verification

An 8-skeptic workflow (`plan11-adversarial-profit-intel`) each tried to break a paper-only rule.
**Round 1: 6 vectors confirmed clean, 1 genuine fix, 1 false positive:**

| Attack | Verdict |
|---|---|
| profit-intel-trades (open a position / call wallet/safe_bet) | **CLEAN** — purely observational; logging runs after the decision |
| future-data-leak (settlement field changes a rank) | **CLEAN** — whitelist design; `ranking_ignores_future_outcome_fields` |
| fake-profit-claim (profitable without Gate 2) | **CLEAN** — `gate_pass=(not reasons) and profitable` is load-bearing; endpoint green only on gate2 pass |
| gate-bypass-via-wiring (logging alters control flow / skips a gate) | **CLEAN** — best-effort, guarded, after the decision; args are in-scope locals |
| stake-inflation (Plan 11 feeds sizing) | **CLEAN** — no Plan 11 module is imported by the sizing path |
| live-db-or-daemon (destructive write / daemon / network) | **CLEAN** — `decision_features` writes only its additive table; `profit_intel` read-only |
| **win-rate without sample guard** (`by_theme_pnl` showed n=1 → 100%) | **FIXED** — `_guard_theme_pnl()` withholds win-rate/ROI for themes < `MIN_SEGMENT_N` (post-processes `theme_pnl` WITHOUT touching the sizing-critical `adaptive.theme_pnl`). Test `theme_pnl_withholds_small_sample_winrate` |
| gate2 "missing elif for status 'fail'" → Gate 2 false pass | **FALSE POSITIVE (verified)** — `audit_accounting` returns only ok/degraded/drift/unknown/error (lines 295–303), all handled by `gate2_status`; it never returns `'fail'`. The "no-bet counted as trade" scenario is caught via the accounting **drift** reconciliation (`gate2_db_drift`), not a 'fail' status. Plan 11 only *consumes* `gate2_status` read-only — it introduces no weakening; Plan 9 gate logic is untouched. |

**Round 2:** 7 vectors clean again; **1 genuine consistency fix** — the round-1 guard was only
applied in `profit_intel`, but `command_center.theme_label_performance`, `metrics.full_report`,
and `scoreboard.profitability_report` still called `adaptive.theme_pnl()` raw, so a tiny-sample
win-rate could still reach `/api/command_center`. **FIXED** — added the public `guarded_theme_pnl()`
and applied it at all three display sites (display-only; `scoreboard`'s `adaptive_min_edge`/sizing
still reads the raw `theme_pnl`, unchanged). Test `display_surfaces_withhold_small_sample_winrate`.

**Round 3:** 7 vectors clean again; **1 finding — DECLINED (out of scope, documented):**
`metrics.paper_metrics()` exposes the OVERALL book `hit_rate`/`roi` without a 20-trade floor, and
this reaches `/api/command_center`. **Declined** because: (1) `paper_metrics` is a pre-existing
Plan-9 core metric, not introduced by Plan 11; (2) its contract is pinned by existing
`test_metrics.py` (`hit_rate==0.5`, `roi`, `profit_factor` as floats on n=4/n=2) — guarding it
in-place would break Plan-9 tests; (3) it always returns `n` alongside, so the sample size is
visible (not a hidden claim); (4) it is the OVERALL book aggregate, not a per-strategy "proven"
claim; (5) the user scoped "do not fix everything / do not refactor the whole project." The
**principle Plan 11 enforces**: *per-segment/strategy* performance claims (theme/source/bucket
win-rate — prone to small-sample illusion) require `MIN_SEGMENT_N` and are guarded everywhere;
the overall portfolio aggregate is shown raw *with its sample size*. Noted in Remaining Risks.

**Final convergence:** core rules (no trading / no future-leak / no fake-profit-without-Gate-2 /
no gate-bypass / no stake-inflation / no live-DB / no-bets-not-failures) were **CLEAN in all 3
rounds**. The only iterative findings were the sample-size-guard class: fixed for every Plan 11
surface + the per-theme strategy win-rate (rounds 1–2), and declined for the pre-existing
contract-pinned aggregate metric (round 3). One round-1 "critical" was a verified false positive.

## Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Candidate ranking exists/hardened | ✅ `opportunity_ranker.rank_candidates` |
| 2 | Decision feature snapshots for bet/no-bet | ✅ `decision_features` + wired into both daemons |
| 3 | Edge explanation available + safe | ✅ `edge_explainer.explain_edge` (no unsafe language) |
| 4 | No-bet intelligence summary | ✅ `summarize_no_bets` |
| 5 | Post-trade learning summary | ✅ `summarize_post_trade_learning` (honesty flags) |
| 6 | Strategy/source attribution + sample warnings | ✅ `attribution` + win-rate gating |
| 7 | Dashboard/report exposes profit intel safely | ✅ `/api/profit-intelligence` (green only on gate2 pass) |
| 8 | Profit intelligence never opens positions | ✅ static scan 0; adversarial CLEAN |
| 9 | Never bypasses Plan 1–10 gates | ✅ best-effort wiring; sizing untouched; adversarial CLEAN |
| 10 | No future-data leakage in pre-forecast ranking | ✅ whitelist + invariance test |
| 11 | No unsafe profit language | ✅ `BANNED_PHRASES` + behavioural + static tests |
| 12 | Paper-only explicit | ✅ flag in every module; every surface `paper_only=true` |
| 13 | Tests prove the above | ✅ 41/41 |
| 14 | Existing tests still pass | ✅ 65/65 modules |
| 15 | Report written | ✅ this file |

**Verdict:** ✅ **PLAN 11 COMPLETE.** All 15 acceptance criteria met; **42/42** profit-intelligence
tests + **65/65** suite modules green. Across **3 adversarial rounds** the core rules held clean
every time (no trading, no future-data leak, no fake-profit-without-Gate-2, no gate-bypass via the
wiring, no stake inflation, no live-DB/daemon, no-bets-as-learning). Genuine findings were all the
sample-size-guard class: fixed for every Plan 11 surface + the per-theme strategy win-rate; the
round-3 finding (pre-existing contract-pinned aggregate metric) was declined with rationale + noted
as a Remaining Risk. Profit intelligence improves ranking / explanation / learning while proven
unable to bypass safety, leak future data, or make fake profit claims.
