# Plan 5 — Same-Day Parity Report

> Goal: `sameday.py` must not be a WEAKER betting path than `predict_today.py` —
> same evidence, same EV penalties, same fail-closed / swarm-health gates, same
> atomic wallet, same no-bet visibility.
>
> Paper-only. No real-money execution. Branch: `fix/sameday-parity-with-predict-today`.
> Built on Plans 1-4 (8ea3920, 353b0cc, 3310b9a, f5e767a).

Status: **COMPLETE** — 59/59 test modules pass (incl. the new 21-case Plan-5 module).
Committed as 29b90e3.

## Summary

Same-day previously forecast **blind** (the swarm + challenger ran before the
evidence pack was built), ran its EV gate **without** the market object or
confidence (so the spread/liquidity/uncertainty/exit-risk penalties never fired),
journaled only *some* of its no-bet decisions (divergence/consensus/evidence/
sizer/event/coherence were print+obs only), had **no explicit stale filter**, and
launched MiroFish fire-and-forget without saying it was unused. Plan 5 brings
same-day to parity with predict_today: build the **same** canonical evidence pack
**before** forecasting and feed it to **both** the swarm and the challenger; run the
EV gate with `m` + `confidence`; add an explicit `scanner.is_stale` filter; route
**every** no-bet through the journaling helper `_sd_skip`; and honestly label
MiroFish as launch-only (`mirofish_used=False`).

## Old Behavior

* **Forecast blind:** `_ai_scout` called `Swarm.forecast(question, market_odds, market_id)`
  and `challenger.ensemble_forecast(question, price)` with NO evidence; the pack was
  built *after* the forecast.
* **EV gate weaker:** `_p7_ev_gate(final_p, price, side)` — no `m`, no `confidence` —
  so the spread/liquidity/uncertainty/exit-risk penalties (and the consensus-as-
  confidence drag) never ran. A thin raw-edge bet that predict_today would reject
  could pass.
* **Partial journaling:** divergence, consensus, evidence, sizer, event-portfolio
  reject, and Guard-D coherence skips were `print` + `obs.on_trade_skip` only — they
  never reached `journal.decisions`, so the dashboard transcript under-counted them.
* **No explicit stale filter:** same-day relied on the (now fail-closed) post-forecast
  risk guard; predict_today's candidate path screens staleness up front.
* **MiroFish ambiguity:** a fire-and-forget launch could look like a contribution.

## New Behavior

* **Evidence before forecast:** `place_sameday` builds the SAME canonical pack
  predict_today uses (`loop.build_pack`) BEFORE `_ai_scout`. A build ERROR → no bet
  (`sameday_evidence_build_error_no_bet`) — same-day never forecasts blind. A built-
  but-thin pack still forecasts (the swarm sees it); the evidence GUARD then gates the
  BET — matching predict_today's order.
* **Context to swarm:** `_ai_scout` passes `extra_context=evidence_text` to
  `Swarm.forecast`.
* **Context to challenger:** `_ai_scout` passes the same evidence to
  `challenger.ensemble_forecast(question, price, evidence_text)` (the challenger
  interface already supported `extra_context` — no interface change needed).
* **Full EV metadata/penalties:** both same-day EV calls now pass `m=m,
  confidence=cons`, so the spread/liquidity/uncertainty/exit-risk penalties run
  exactly as in predict_today. A +edge bet whose after-cost-and-penalty EV is
  negative is now rejected.
* **Stale/quality parity:** an explicit `scanner.is_stale(m)` filter runs BEFORE the
  forecast (`sameday_stale_market_no_bet`); if staleness can't be evaluated →
  `sameday_market_quality_unknown_no_bet` (no forecast on unknown).
* **Every no-bet journaled:** all branches route through `_sd_skip` (print +
  `obs.on_trade_skip` + `journal.record_decision(action="no_bet")`) with
  `sameday_`-prefixed reasons.
* **MiroFish honesty:** the launch is labeled `mirofish_launched_not_used`, an obs
  note records `mirofish_used=False`, and the scout health dict carries
  `mirofish_used=False` / `evidence_used`. No same-day decision claims MiroFish
  contributed.
* **Wallet open:** unchanged and at parity with predict_today — both daemons run the
  full gate stack INLINE and then call the Plan-4 atomic `wallet.open_position`
  (race-safe, guarded). (Neither daemon uses `safe_bet`; that is reserved for the
  shortcut paths — strategy_bet/loop/place_bet. See Remaining Risks.)

## Predict Today vs Same-Day Parity Matrix

| Stage | predict_today | OLD sameday | gap | NEW sameday |
| ----- | ------------- | ----------- | --- | ----------- |
| candidate filter | classifier + liquidity + horizon | opinion + liquidity + hours | minor | unchanged |
| stale filter | market-quality (scanner.is_stale) | none explicit (post-forecast guard only) | YES | explicit `scanner.is_stale` BEFORE forecast |
| evidence pack | built then fed to forecast | built AFTER forecast | YES | built BEFORE forecast; build-error → no bet |
| swarm forecast | `extra_context=enr` | no context (blind) | YES | `extra_context=evidence_text` |
| challenger forecast | `ensemble_forecast(q,price,enr)` | `ensemble_forecast(q,price)` (blind) | YES | `ensemble_forecast(q,price,evidence_text)` |
| swarm health (Plan 2) | `_p_swarm_health` | `_p_swarm_health` | none | unchanged (journaled) |
| divergence guard | journaled skip | print+obs only | YES | `_sd_skip` → `sameday_divergence_no_bet` |
| consensus guard | journaled skip | print+obs only | YES | `_sd_skip` → `sameday_consensus_no_bet` |
| evidence guard | `_evidence_guard` skip | print+obs only | YES | `_sd_skip` → `sameday_no_evidence_/low_evidence_quality_no_bet` |
| EV gate | `m`+`confidence` | NO m/confidence | YES | `m=m, confidence=cons` |
| risk guard | `_p8_risk_guards` | `_p8_risk_guards` | none | journaled (Plan 1) |
| bankroll guard | `_p9_can_trade` | `_p9_can_trade` | none | journaled (Plan 1) |
| exposure guard | `_p9_exposure_ok` | `_p9_exposure_ok` | none | journaled (Plan 1) |
| event-portfolio reject | journaled | print+obs only | YES | `_sd_skip` → `sameday_event_portfolio_no_bet` |
| sizer no-edge | journaled | print+obs only | YES | `_sd_skip` → `sameday_no_edge_no_bet` |
| Guard-D coherence | journaled | print+obs only | YES | `_sd_skip` → `sameday_event_*_no_bet` |
| wallet open | atomic `open_position` (inline-gated) | same | none | same; rejection → `sameday_wallet_rejected_no_bet` |
| MiroFish | real gate (reads report) | launch-only, unlabeled | YES | launch-only LABELED `mirofish_launched_not_used` / `mirofish_used=False` |
| no-bet journaling | journal.decisions | partial | YES | every branch via `_sd_skip` |

## New No-Bet Reasons

`sameday_stale_market_no_bet`, `sameday_market_quality_unknown_no_bet`,
`sameday_evidence_build_error_no_bet`, `sameday_no_evidence_no_bet`,
`sameday_low_evidence_quality_no_bet`, `sameday_divergence_no_bet`,
`sameday_consensus_no_bet`, `sameday_event_portfolio_no_bet`,
`sameday_no_edge_no_bet`, `sameday_event_already_hold_yes_no_bet`,
`sameday_event_incoherent_no_bet`, `sameday_wallet_rejected_no_bet`.
Honesty marker: `mirofish_launched_not_used` (obs) / `mirofish_used=False` (health).
(The money-gate skips keep their specific Plan 1/2 reasons — e.g. `neg_ev_after_costs`,
`risk_guards_error_fail_closed`, `sameday_swarm_*_no_bet` — now all journaled.)

## Tests Added

`harness/tests/test_sameday_parity.py` (21): ai_scout_passes_evidence_to_swarm_and_challenger;
evidence_is_built_before_scout; evidence_build_error_blocks_and_does_not_forecast;
no_evidence_blocks; low_evidence_quality_blocks; ev_gate_receives_market_and_confidence;
ev_blocks_after_spread_penalty_with_real_gate; stale_market_skipped_before_forecast;
unknown_market_quality_blocks; divergence/consensus/swarm_health/ev/risk/bankroll/exposure/
wallet_rejection skips journaled; mechanical_skip_not_scouted; mirofish_marked_not_used;
healthy_path_reaches_wallet_open; healthy_path_real_wallet_opens_one_position.

## Commands Run / Test Results

See `PLAN5_COMMAND_LOG.md`. `test_sameday_parity` 21/21 (exit 0);
`python run_tests.py --no-llm` → **59/59 modules passed** (exit 0, no FAIL, no skips
beyond the pre-existing LLM-integration self-skip). pytest not used. No live-service /
live-DB / trading commands run.

## Static verification (Phase 12)

| Search | Finding | Safe? | Explanation |
| ------ | ------- | ----- | ----------- |
| `extra_context=evidence_text` | swarm forecast (sameday.py:191) | yes | swarm sees the evidence pack |
| challenger `ensemble_forecast(... evidence_text)` | _ai_scout | yes | challenger sees the same evidence (test-proven) |
| `m=m, confidence=cons` | both EV gates (418, 482) | yes | full spread/liquidity/uncertainty/exit-risk penalties run |
| `scanner.is_stale` | pre-forecast (309) | yes | stale/unknown markets skipped before the slow forecast |
| `_sd_skip(` | every no-bet branch | yes | all skips print+obs+journal |
| `mirofish_launched_not_used` / `mirofish_used=False` | _ai_scout | yes | MiroFish honestly labeled launch-only |
| `wallet.open_position(` | 2 inline-gated opens | yes | atomic Plan-4 opener after the full gate stack (parity with predict_today) |

## Remaining Risks (Plan 5 only)

* **Same-day opens via inline-gated `wallet.open_position`, not `safe_bet`.** This is
  DELIBERATE parity with predict_today, which also gates inline and does not use
  `safe_bet` (that helper is for the shortcut paths — strategy_bet/loop/place_bet).
  Both daemons run swarm-health → EV → risk → bankroll → exposure inline and then
  call the Plan-4 atomic, race-safe `wallet.open_position`. Routing same-day through
  `safe_bet` would double-run the gates and DIVERGE from predict_today, so it was not
  done. The wallet open is fully controlled either way.
* **Evidence pack is built per-candidate (network GATHER) before the forecast** — same
  cost as before (the pack was already built each candidate), just reordered, so the
  forecast now sees it.
* **MiroFish is still launch-only in same-day** (not read into the decision) — Plan 5
  only makes that HONEST (`mirofish_used=False`); full same-day MiroFish integration
  is out of scope (a later plan).
* **Mechanical/classifier skips** are recorded via `obs.on_classify` (parity with
  predict_today's loop), NOT journaled to `decisions`, to avoid flooding the table
  every cycle.

## Proof

* **Evidence passed before swarm** — `_ai_scout` forwards `extra_context` to
  `Swarm.forecast` and `ensemble_forecast` (`ai_scout_passes_evidence_to_swarm_and_challenger`);
  the pack reaches the scout (`evidence_is_built_before_scout`); a build error blocks
  WITHOUT forecasting (`evidence_build_error_blocks_and_does_not_forecast`).
* **Evidence passed before challenger** — same test asserts `chal_ctx == evidence`
  (the challenger interface already supported it; no limitation).
* **EV penalties apply** — `ev_gate_receives_market_and_confidence` (m + confidence
  passed); `ev_blocks_after_spread_penalty_with_real_gate` (a +0.05 edge with a 12c
  spread is rejected — would have passed without `m`).
* **Skips are journaled** — divergence/consensus/evidence/EV/risk/bankroll/exposure/
  wallet + stale + market-quality-unknown all produce a `decisions` `no_bet` row.
* **Wallet opening controlled** — `healthy_path_reaches_wallet_open` +
  `healthy_path_real_wallet_opens_one_position` (one atomic open after all gates pass).
* **MiroFish not-used is honest** — `mirofish_marked_not_used`
  (`mirofish_launched_not_used` + `mirofish_used=False`; no decision claims it was used).

## Phase 14 — acceptance criteria

1. same-day builds evidence before forecast — YES.
2. evidence context passed to swarm — YES.
3. evidence context passed to challenger — YES (interface already supported it).
4. blocks on evidence failure / low quality — YES.
5. EV gate uses full metadata/confidence — YES.
6. stale/market-quality not weaker than predict_today — YES (explicit pre-forecast filter).
7. no-bet reasons journaled, not only printed — YES (all via `_sd_skip`).
8. wallet opening controlled — YES (full inline gate stack → atomic open_position, parity with predict_today).
9. MiroFish launch-only labeled not-used — YES.
10. tests prove the parity fixes — YES (21 cases).
11. existing tests still pass — YES (59/59).
12. report written — YES (this file).
