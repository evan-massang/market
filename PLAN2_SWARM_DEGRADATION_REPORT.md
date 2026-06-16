# Plan 2 — Swarm Degradation Safety Report

> Goal: a degraded swarm forecast must NEVER look like a healthy high-consensus
> forecast, and must never reach `wallet.open_position`.
>
> Paper-only. No real-money execution. Branch: `fix/swarm-degradation-safety`.
> Plan 1 (fail-closed money gates, commit 8ea3920) is the committed base.

Status: **IN PROGRESS**.

## Swarm chain map (Phase 1)

| Step | File/function | Current behavior | New behavior |
| ---- | ------------- | ---------------- | ------------ |
| 1. agents return estimates | `core/agent.py::Agent.estimate` | raises on no usable prob; swarm catches per-agent | unchanged; swarm now records the failure in `agent_failures` |
| 2. aggregate mean/variance/consensus | `core/aggregator.py::aggregate` | 1 estimate → std_dev 0 → **consensus_score=1.0** | n<MIN_FOR_CONSENSUS → consensus 0.0 + `consensus_status="insufficient_agents"`; sample-size dampener so 2 agents can't read as max confidence; 3+ unchanged |
| 3. swarm returns result | `core/swarm.py::Swarm.forecast` | normal path: no degraded/allow_bet/aborted; abort/all-failed: partial flags | every path sets `degraded/aborted/allow_bet/degradation_reason/n_agents_requested/succeeded/failed/agent_failures` via `core.swarm_health.assess` |
| 4. loop extracts (prob, meta) | `harness/loop.py::_forecast` | meta = {regime, consensus} only | meta also carries aborted/degraded/allow_bet/n_agents_*/degradation_reason/method |
| 5. predict_today guards | `harness/predict_today.py::predict_one` | consensus guard only; consensus 1.0 passes | new `_p_swarm_health(meta)` guard BEFORE sizing/wallet blocks aborted/degraded/insufficient/missing-metadata/fallback |
| 6. sameday guards | `harness/sameday.py::place_sameday` | consensus guard only | `_ai_scout` returns health; `_p_swarm_health(..., prefix="sameday_swarm")` blocks before sizing/wallet |
| 7. sizing / wallet open | `sizing.size_bet` → `wallet.open_position` | reached by degraded swarm | unreachable when swarm-health gate blocks (proven by tests + structural scan) |
| (persist) | `core/calibration.py::save_swarm_forecast` + `swarm_forecasts` | stored without degraded flag | new columns `degraded/n_agents_succeeded/n_agents_requested/degradation_reason` (backward-compatible) |

---

Status: **COMPLETE** — 56/56 test modules pass (incl. the new 20-case Plan-2 module).
Branch `fix/swarm-degradation-safety` (not committed — awaiting review).

## Summary

A degraded swarm forecast (too few agents survived, or the run aborted) could
previously look like a healthy high-consensus forecast and place a paper bet. Plan
2 makes swarm strength explicit and enforced: the aggregator can no longer turn one
surviving agent into `consensus_score=1.0`; the swarm attaches honest health flags
(`degraded`/`aborted`/`allow_bet`/`n_agents_*`/`agent_failures`) on every return
path; `loop._forecast` stops dropping that metadata; and a new `_p_swarm_health`
gate in BOTH predict_today and sameday blocks a degraded/aborted/under-strength
swarm before sizing or `wallet.open_position`. Degraded forecasts are persisted as
degraded. Default policy: **fewer than 3 surviving agents → no bet**.

## Old Dangerous Behavior

* **One survivor = perfect consensus.** `aggregator.aggregate` computed
  `consensus = 1.0 - std_dev*2`; a single estimate has `std_dev=0`, so one voice
  read as `consensus_score=1.0` ("perfect agreement"), which sailed through the
  `MIN_SWARM_CONSENSUS` guard and drove conviction sizing.
* **Dropped metadata.** `loop._forecast` returned `meta = {regime, consensus}` only,
  discarding `aborted`/`degraded`/`n_agents`/`method` — so predict_today/sameday had
  no way to know the swarm was degraded.
* **Degraded reached betting.** The swarm's normal path set no health flags; sameday's
  `_ai_scout` returned only `(sp, bp, cons, final_p)`. A 1–2 agent forecast could be
  sized and bet like a full swarm.

## New Behavior

* **Minimum surviving-agent policy** (`core/swarm_health.py`):
  `MIN_SWARM_AGENTS_FOR_BET=3`, `MIN_SWARM_AGENTS_FOR_CONSENSUS=2` (env-overridable).
  `allow_bet` is True only when a strict MAJORITY of requested agents survived AND
  at least 3 did — so a 5-agent swarm bets at 3/4/5, but a 6-agent swarm does NOT bet
  at a 3/3 coin-flip split.
* **Consensus with 1/2/3+ agents.** 1 survivor → `consensus_score=0.0` +
  `consensus_status="insufficient_agents"` (never 1.0). 2 survivors → consensus is
  computed but a sample-size dampener (×0.5) prevents it reading as max confidence
  + `consensus_status="limited_agents"`. 3+ → full, UNCHANGED consensus.
* **predict_today blocks degraded swarm** via `_p_swarm_health(meta)` placed BEFORE
  the reliability guards, sizing, the event-portfolio path, and `wallet.open_position`.
* **sameday blocks degraded swarm** via the same guard (`prefix="sameday_swarm"`),
  reached from `_ai_scout`'s new health 5-tuple, recorded through `_sd_skip`
  (print + obs.on_trade_skip + journal).
* **Degraded forecasts stored honestly.** `swarm_forecasts` gained backward-compatible
  `degraded`/`n_agents_succeeded`/`n_agents_requested`/`degradation_reason` columns;
  the swarm populates them. Aborted/all-failed runs return BEFORE persistence, so no
  fake-healthy row is ever written for them.

## Files Changed

| File | Purpose |
| ---- | ------- |
| `core/swarm_health.py` (NEW) | Policy constants + `assess(n_requested,n_succeeded)` (degraded/aborted/allow_bet/reason/counts) + `consensus_allowed` + `consensus_size_factor`. Pure, no cycle. |
| `core/aggregator.py` | Lone survivor → consensus 0.0; sample-size dampener for <MIN_FOR_BET; `consensus_status`/`consensus_degraded`/`raw_consensus_score`/`n_agents_used`; empty input returns safe degraded dict (no raise). 3+ agents unchanged. |
| `core/swarm.py` | Collect `agent_failures`; merge `assess()` health into the result on ALL paths (no-agents abort, all-failed, normal); persist degraded fields. |
| `core/calibration.py` | `swarm_forecasts` degraded columns (via `_ensure_column`); `save_swarm_forecast` optional degraded kwargs (backward-compatible). |
| `harness/loop.py` | `_forecast` meta now carries aborted/degraded/allow_bet/n_agents_*/degradation_reason/method; dry-run stub gets a complete healthy block. |
| `harness/predict_today.py` | `_p_swarm_health(meta, prefix)` guard + `_swarm_health_skip_reason`; wired into `predict_one` before all bet paths. |
| `harness/sameday.py` | `_ai_scout` returns a 5th `health` value (all 3 returns); swarm-health guard in `place_sameday` before all bet paths. |
| `harness/tests/test_swarm_degradation.py` (NEW) | 20 cases: aggregator, assess policy, swarm offline-integration, predict_today + sameday guards, no-open proof, structural scan, persistence. |

## New No-Bet Reasons

predict_today: `swarm_aborted_no_bet`, `swarm_degraded_no_bet`,
`swarm_insufficient_agents_no_bet`, `swarm_missing_health_metadata_no_bet`,
`swarm_fallback_probability_no_bet`.
sameday (same logic, prefixed): `sameday_swarm_aborted_no_bet`,
`sameday_swarm_degraded_no_bet`, `sameday_swarm_insufficient_agents_no_bet`,
`sameday_swarm_missing_health_metadata_no_bet`, `sameday_swarm_fallback_probability_no_bet`.
swarm-level `degradation_reason`: `no_agents_succeeded`, `insufficient_surviving_agents`.

## Tests Added

`harness/tests/test_swarm_degradation.py` (20): agg_one_estimate_not_consensus_one,
agg_one_estimate_marked_insufficient, agg_two_estimates_not_max_confidence,
agg_three_estimates_normal_consensus, agg_empty_is_safe, assess_policy_counts,
swarm_zero_agents_aborts, swarm_all_failed_aborts, swarm_one_succeeds_is_degraded_no_bet,
swarm_three_succeed_allow_bet, pt_guard_blocks_each_degraded_case, pt_guard_allows_healthy,
pt_skip_records_no_bet_and_never_opens, sameday_guard_uses_prefixed_reasons,
sameday_guard_allows_healthy, sameday_sd_skip_records_no_bet_and_never_opens,
ai_scout_returns_five_tuple_on_early_skip, guard_precedes_open_position_in_both_files,
persist_degraded_marked, persist_healthy_not_degraded.

## Commands Run / Test Results

See `PLAN2_COMMAND_LOG.md`. `test_swarm_degradation` 20/20 (exit 0);
`python run_tests.py --no-llm` → **56/56 modules passed** (exit 0, no FAIL, no skips
beyond the pre-existing LLM-integration self-skip in test_swarm_sizes). pytest not used.
No live-service / live-DB / trading commands run.

## Phase 11 — verification matrix

| Case | Expected result | Test proving it |
| ---- | --------------- | --------------- |
| 0 agents | no bet (aborted) | `swarm_zero_agents_aborts`, `assess_policy_counts`, `pt_guard_blocks_each_degraded_case` |
| 1 agent | no bet (degraded, consensus≠1.0) | `swarm_one_succeeds_is_degraded_no_bet`, `agg_one_estimate_not_consensus_one` |
| 2 agents | no bet by default | `assess_policy_counts`, `agg_two_estimates_not_max_confidence` |
| 3 agents | allowed only if other gates pass | `swarm_three_succeed_allow_bet`, `assess_policy_counts` |
| 5 agents | normal (allow, not degraded) | `assess_policy_counts`, `pt_guard_allows_healthy` |
| missing metadata | no bet | `pt_guard_blocks_each_degraded_case` (`{}` and missing-key cases) |
| degraded reaching wallet | impossible | `pt_skip_..._never_opens`, `sameday_..._never_opens`, `guard_precedes_open_position_in_both_files` |

## Remaining Risks (Plan 2 only)

* **Gate-1 / calibration filtering.** Degraded forecasts are now FLAGGED in
  `swarm_forecasts` (degraded + counts + reason), but the scoreboard's Gate-1 query
  was intentionally not modified (out of Plan-2 scope). The data needed to exclude
  degraded rows is now present; wiring that filter is a small follow-up.
* **Per-size label nuance.** The spec's 6-agent "5–6 = healthy" cosmetic label is
  reported as `degraded=True` for any run that lost an agent (more conservative /
  honest). This does NOT change betting — `allow_bet` matches the spec's allow/no-bet
  column for every row (5-agent bets at 3/4/5; 6-agent does not bet at 3/3).
* `consensus_size_factor` halves the consensus for exactly-2 survivors; those runs
  are non-bettable anyway (`allow_bet=False`), so it only affects the (already blocked)
  degraded display value, never a real bet.

## Proof

* **0 agents cannot bet** — `Swarm([]).forecast(...)` → `aborted=True, allow_bet=False`;
  `_p_swarm_health` → `swarm_aborted_no_bet`.
* **1 agent cannot bet** — swarm → `degraded=True, allow_bet=False, n_succeeded=1`,
  `consensus_score<1.0`; guard → `swarm_insufficient_agents_no_bet`.
* **2 agents cannot bet by default** — `assess(5,2).allow_bet is False`; aggregator
  consensus is damped (<1.0).
* **Missing metadata cannot bet** — `_p_swarm_health({})` → `swarm_missing_health_metadata_no_bet`.
* **Healthy swarm still works** — `_p_swarm_health(healthy)` → `(True,"ok")`;
  `Swarm` with 3 varied agents → `allow_bet=True`; 3+ agents keep their full,
  unchanged consensus score.
* **No degraded swarm reaches `wallet.open_position`** — `_skip`/`_sd_skip` never open
  a position (asserted by patching `open_position` to raise), and a source scan proves
  the swarm-health guard call precedes every `wallet.open_position` in both files.

## Phase 13 — acceptance criteria

1. one survivor cannot be consensus 1.0 — YES (`agg_one_estimate_not_consensus_one`).
2. fewer than minimum agents blocks betting — YES (`assess_policy_counts`, guard tests).
3. zero-agent fallback/default probability cannot bet — YES (`swarm_all_failed_aborts`,
   `pt_guard_blocks_each_degraded_case` fallback case).
4. degraded metadata preserved downstream — YES (`loop._forecast` passthrough; sameday 5-tuple).
5. predict_today blocks degraded swarm before sizing/wallet — YES (guard placed pre-bet; scan).
6. sameday blocks degraded swarm before sizing/wallet — YES (guard placed pre-bet; scan).
7. degraded block visible in journal/log/obs — YES (`_skip`/`_sd_skip`).
8. wallet.open_position never called on degraded swarm — YES (no-open tests + scan).
9. tests prove all failure cases — YES (20 cases).
10. existing happy-path tests still pass — YES (56/56).
11. report written — YES (this file).
