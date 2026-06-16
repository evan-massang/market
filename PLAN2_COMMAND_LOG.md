# Plan 2 — Command Log

Every command for Plan 2 (swarm degradation / fake-confidence safety). Paper-only.
Branch: `fix/swarm-degradation-safety`.

## Phase 0 — prep

```
(Get-Location).Path            -> C:\Users\OMEN\Pictures\Polymarket\polyswarm
git rev-parse --abbrev-ref HEAD-> fix/swarm-degradation-safety  (branch already existed; cut from the Plan-1 commit)
git status --short             -> only ?? agentdb.rvf / agentdb.rvf.lock (pre-existing ruflo artifacts)
git log --oneline -1 8ea3920   -> 8ea3920 fix(safety): fail-closed money gates  (Plan 1 IS committed)
```
Plan 1 is committed (8ea3920) and contained in this branch -> safe to proceed. Not creating a
new branch (already on fix/swarm-degradation-safety). agentdb.rvf* left untouched.

## Phase 1 — locate swarm aggregation + betting chain (Grep/Read, read-only)

Read in full: core/aggregator.py, core/swarm.py, core/agent.py, harness/loop.py (_forecast/build_pack),
harness/predict_today.py (forecast extraction + guards), harness/sameday.py (_ai_scout + place_sameday),
core/calibration.py (save_swarm_forecast + schema), harness/tests/test_swarm_sizes.py.

FAKE-CONFIDENCE / DROPPED-METADATA sites:
  core/aggregator.aggregate  L42  consensus = 1 - std_dev*2  -> ONE estimate => std_dev 0 => consensus 1.0
  core/swarm.forecast        normal path sets NO degraded/allow_bet/aborted; n_agents = succeeded only
  core/swarm.forecast        abort (L91) + all-failed (L215) paths set partial flags only
  harness/loop._forecast     L161 returns meta = {regime, consensus} ONLY (drops aborted/degraded/n_agents/method)
  harness/predict_today      L892 _betting_guards uses meta.get("consensus"); consensus 1.0 PASSES the guard
  harness/sameday._ai_scout  L197 returns (sp,bp,cons,final_p); drops degraded/aborted/n_agents

## Phases 2-9 — implementation (no shell)

NEW core/swarm_health.py (policy: MIN_SWARM_AGENTS_FOR_BET=3 / _FOR_CONSENSUS=2 + assess()
+ consensus_size_factor). Edited core/aggregator.py (lone-survivor can't be consensus 1.0;
sample-size dampener; empty-safe), core/swarm.py (agent_failures + assess() merged on all 3
return paths + degraded persisted), core/calibration.py (swarm_forecasts degraded/n_agents_*
columns + save_swarm_forecast kwargs), harness/loop.py (_forecast meta passthrough + dry-run
stub), harness/predict_today.py (_p_swarm_health guard + wired before bet paths),
harness/sameday.py (_ai_scout returns health 5-tuple + guard before bet paths).
NEW harness/tests/test_swarm_degradation.py (20 cases).

## Phases 10-11 — test + static verification commands

```
# interpreter: .\.venv\Scripts\python.exe
python -m harness.tests.test_swarm_degradation        -> 20/20 passed (exit 0)
python run_tests.py --no-llm                          -> SUMMARY: 56/56 modules passed (exit 0, no FAIL)
```
pytest not used (run_tests.py is the supported stdlib runner). NOT run (per constraints):
supervisor start/stop, live daemons, real betting loop, db_check --repair.

Static (Grep, read-only):
```
grep "consensus_score = 1.0" in core/   -> No matches (the fake-consensus assignment is gone)
grep allow_bet|n_agents_succeeded|aborted in predict_today/sameday/loop
  -> set in loop._forecast (real + dry-run), read by _p_swarm_health guard in both bet paths
```

