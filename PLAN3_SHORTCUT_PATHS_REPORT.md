# Plan 3 — Shortcut Betting Paths Report

> Goal: NO code path can call `wallet.open_position` / create a paper bet without
> passing the same safety stack (swarm-health → EV → risk → bankroll → exposure),
> or being disabled-by-default / test-only.
>
> Paper-only. No real-money execution. Branch: `fix/gate-shortcut-betting-paths`.
> Built on Plan 1 (fail-closed gates, 8ea3920) + Plan 2 (swarm degradation, 353b0cc).

Status: **COMPLETE** — committed as 3310b9a.

## Entrypoint Map (Phase 1)

| Entrypoint | File/function | Can open position? | Current safety gates | Missing gates | Action |
| ---------- | ------------- | -----------------: | -------------------- | ------------- | ------ |
| strategy_bet | `harness/strategy_bet.py::main` | YES | wallet per-bet/exposure cap ONLY | EV, risk, bankroll, exposure-cap, swarm-health | **Disable by default** (`ENABLE_STRATEGY_BET=false`); if enabled, route through `safe_bet` |
| legacy loop | `harness/loop.py::run_once` | YES | none (size_bet only) | EV, risk, bankroll, exposure, swarm-health; can bet fallback 0.5 | **Disable betting by default** (`ENABLE_LEGACY_LOOP_BETTING=false`); keep settle/score; if enabled, route through `safe_bet` |
| manual place_bet | `harness/place_bet.py::main` | YES | none (size_bet only) | EV, risk, bankroll, exposure, swarm-health | Route through `safe_bet` (has a real forecast) |
| predict_today | `harness/predict_today.py::predict_one` | YES | FULL Plan 1+2 stack inline | — | ALLOWED (unchanged) |
| sameday | `harness/sameday.py::place_sameday` | YES | FULL Plan 1+2 stack inline | — | ALLOWED (unchanged) |
| wallet | `harness/wallet.py::open_position` | (the primitive) | per-bet/exposure cap | — | low-level primitive; only called by the above |
| tests | `harness/tests/*`, `harness/test_wallet.py` | YES (temp DB) | n/a | n/a | TEST-ONLY (temp DB via make_temp_env) |

Launchers: `harness_pass.bat` schedules `strategy_bet` (now disabled-by-default) + `loop settle`
(safe). The supervisor (`services.py`) runs only the gated predict_today + sameday daemons,
dashboard, and mirofish backend — no shortcut betting.

---

Status: **COMPLETE** — 57/57 test modules pass (incl. the new 14-case Plan-3 module).
Branch `fix/gate-shortcut-betting-paths` (committed as 3310b9a).

## Summary

There were three production code paths that opened paper positions WITHOUT the
Plan 1 + Plan 2 safety stack: `strategy_bet` (zero gates, scheduled by
`harness_pass.bat`), `loop.run_once` (no gates; could bet a fallback 0.5), and the
manual `place_bet` (no gates). Plan 3 introduces ONE shared, safety-gated opener
(`harness/safe_bet.open_position_if_safe`) that every non-test path now goes
through, disables the two legacy shortcuts by default, removes the ungated strategy
line from the scheduled launcher, and adds a repo-wide test proving no uncontrolled
`wallet.open_position` exists outside the allowed files.

## Old Dangerous Behavior

* **strategy_bet** decided a favorite-longshot bet and called `wallet.open_position`
  directly — bypassing EV, risk guard, bankroll kill switch, exposure cap, and
  swarm-health. It was scheduled by `harness_pass.bat` (`strategy_bet --max 60`).
* **loop.run_once** sized + opened a position with NO safety stack; because
  `_forecast` can return a fallback/default 0.5 when the LLM fails, the loop could
  bet on a non-signal.
* **place_bet** (manual single-market) opened directly after `size_bet`, no gates.
* Net: a paper bet could be created on multiple paths that never saw the safety
  stack the predict_today/sameday daemons enforce.

## New Behavior

* **Allowed betting paths:** `predict_today` and `sameday` (full Plan 1+2 stack
  inline, unchanged), and any path routed through `safe_bet.open_position_if_safe`.
* **`harness/safe_bet.py`** runs the SAME gates in the same order, reusing the exact
  predict_today wrappers: **swarm-health (AI sources) → EV → risk → bankroll →
  exposure → open exactly once.** Any block → no `open_position`, structured no-bet
  result, journaled + obs-logged with the exact gate reason.
* **strategy_bet: disabled by default** (`ENABLE_STRATEGY_BET=false`). Returns
  `strategy_bet_disabled_by_default` BEFORE any network fetch. When opted in, it
  routes through `safe_bet`; a bet with no honest probability emits
  `strategy_bet_missing_ev_probability_no_bet`; gate blocks emit
  `strategy_bet_gate_blocked_no_bet`. (A negative-EV longshot is now rejected.)
* **legacy loop betting: disabled by default** (`ENABLE_LEGACY_LOOP_BETTING=false`).
  Settlement/scoring (`loop settle`, the forecast + decision logging) are KEPT; only
  the bet-opening is gated. When opted in, a fallback/degraded forecast is blocked
  (`legacy_loop_fallback_probability_no_bet`) and real bets route through `safe_bet`.
* **place_bet** routes through `safe_bet` (it has a real swarm forecast).
* **Launchers:** `harness_pass.bat` is settlement-only by default (the strategy line
  is commented with explicit opt-in instructions). The supervisor (`services.py`)
  only runs the gated predict_today + sameday daemons, dashboard, and mirofish
  backend — it never started a shortcut betting path.
* **Default env flags** (both safe): `ENABLE_STRATEGY_BET=false`,
  `ENABLE_LEGACY_LOOP_BETTING=false` (documented in `.env.example` + `config_check`).

## Entrypoint Map

(see the Phase-1 table above)

## wallet.open_position Control Map (Phase 7)

| File | Function | Direct open_position? | Safety status |
| ---- | -------- | --------------------: | ------------- |
| `harness/safe_bet.py` | `open_position_if_safe` | YES (1) | THE controlled opener — runs swarm-health/EV/risk/bankroll/exposure first |
| `harness/predict_today.py` | `predict_one` | YES (2) | gated inline by full Plan 1+2 stack (allowed) |
| `harness/sameday.py` | `place_sameday` | YES (2) | gated inline by full Plan 1+2 stack (allowed) |
| `harness/wallet.py` | `open_position` (def) | — | the primitive; only reached via the above |
| `harness/strategy_bet.py` | main | NO (now via safe_bet) | disabled by default; routed when enabled |
| `harness/loop.py` | run_once | NO (now via safe_bet) | betting disabled by default; routed when enabled |
| `harness/place_bet.py` | main | NO (now via safe_bet) | routed through safe_bet |
| `harness/test_wallet.py`, `harness/tests/*` | — | YES (temp DB) | TEST-ONLY |

A repo-wide test (`no_uncontrolled_open_position_calls`) enforces that only
`safe_bet.py`, `predict_today.py`, `sameday.py` may contain a `wallet.open_position(`
call outside test files.

## New No-Bet Reasons

`strategy_bet_disabled_by_default`, `strategy_bet_missing_ev_probability_no_bet`,
`strategy_bet_gate_blocked_no_bet`, `legacy_loop_betting_disabled_by_default`,
`legacy_loop_fallback_probability_no_bet`, `shortcut_path_blocked_no_bet`
(all defined in `harness/safe_bet.py`). Plus every Plan 1/2 gate reason flows
through `safe_bet` for routed bets (e.g. `neg_ev_after_costs`,
`risk_guards_error_fail_closed`, `swarm_insufficient_agents_no_bet`).

## Tests Added

`harness/tests/test_shortcut_paths.py` (14): safebet_all_gates_pass_opens_once,
safebet_each_gate_blocks_no_open, safebet_ai_source_missing_health_blocks,
strategy_bet_disabled_by_default_no_open_no_fetch, strategy_bet_missing_probability_no_bet,
strategy_bet_enabled_ev_blocks_longshot, strategy_bet_enabled_opens_only_after_gates_pass,
legacy_loop_disabled_by_default_no_open, legacy_loop_fallback_probability_no_bet,
legacy_loop_enabled_healthy_opens_via_safe_bet, no_uncontrolled_open_position_calls,
strategy_and_loop_gate_before_open, harness_pass_bat_does_not_run_strategy_by_default,
supervisor_services_do_not_run_shortcut_betting.

## Commands Run / Test Results

See `PLAN3_COMMAND_LOG.md`. `test_shortcut_paths` 14/14 (exit 0);
`python run_tests.py --no-llm` → **57/57 modules passed** (exit 0, no FAIL, no skips
beyond the pre-existing LLM-integration self-skip). pytest not used. No live-service /
live-DB / trading commands run.

## Static verification (Phase 11)

| Search | Remaining count | Safe? | Why |
| ------ | --------------: | ----- | --- |
| `wallet.open_position(` (prod) | 5 | yes | safe_bet (1, controlled) + predict_today (2) + sameday (2), all gated |
| `strategy_bet` direct open | 0 | yes | routed through safe_bet; disabled by default |
| `loop.run_once` direct open | 0 | yes | routed through safe_bet; disabled by default |
| `place_bet` direct open | 0 | yes | routed through safe_bet |
| `ENABLE_STRATEGY_BET` default | false | yes | disabled unless explicitly opted in |
| `ENABLE_LEGACY_LOOP_BETTING` default | false | yes | disabled unless explicitly opted in |
| launcher schedules strategy by default | 0 | yes | harness_pass.bat strategy line commented; supervisor never ran it |

## Remaining Risks (Plan 3 only)

* When `ENABLE_STRATEGY_BET=true`, strategy bets are EV/risk/bankroll/exposure-gated
  but carry NO swarm-health (they are a price rule, not an AI forecast). This is
  intentional and the EV gate rejects the typical negative-EV longshot; operators
  should treat enabling it as opt-in to a price strategy, not an AI forecast.
* `safe_bet` reuses the predict_today gate wrappers via a lazy import; if
  `predict_today` failed to import, `safe_bet` would raise rather than silently bet
  (no bet is opened) — acceptable (fail-closed direction).

## Proof

* **strategy_bet cannot bet by default** — `main([])` with the env unset returns
  `strategy_bet_disabled_by_default` and never fetches markets nor opens
  (`strategy_bet_disabled_by_default_no_open_no_fetch`).
* **legacy loop cannot bet by default** — `run_once` records
  `legacy_loop_betting_disabled_by_default` and opens nothing
  (`legacy_loop_disabled_by_default_no_open`).
* **fallback 0.5 cannot bet** — even with legacy betting enabled, a
  `degraded_all_agents_failed` forecast blocks with
  `legacy_loop_fallback_probability_no_bet` (`legacy_loop_fallback_probability_no_bet`).
* **shortcut gate failure cannot bet** — `safe_bet` blocks on each of EV / risk /
  bankroll / exposure with `open_position` patched to raise; it is never called
  (`safebet_each_gate_blocks_no_open`); an enabled strategy longshot is EV-rejected
  (`strategy_bet_enabled_ev_blocks_longshot`).
* **direct wallet.open_position is controlled** — repo scan allows it only in
  safe_bet / predict_today / sameday (`no_uncontrolled_open_position_calls`).
* **normal paths still work** — `safe_bet` opens exactly once when all gates pass
  (`safebet_all_gates_pass_opens_once`); an enabled, fully-gated +EV strategy NO-fade
  opens (`strategy_bet_enabled_opens_only_after_gates_pass`); an enabled, healthy
  legacy loop opens via safe_bet (`legacy_loop_enabled_healthy_opens_via_safe_bet`);
  predict_today / sameday retain their inline Plan 1+2 stack (57/57 incl. their tests).

## Phase 13 — acceptance criteria

1. strategy_bet cannot bypass money gates — YES (routes through safe_bet; disabled by default).
2. strategy_bet disabled by default OR fully gated — YES (both: off by default, gated if enabled).
3. legacy run_once cannot bet from fallback/default probability — YES (`legacy_loop_fallback_probability_no_bet`).
4. legacy loop betting disabled by default OR gated — YES (both).
5. no launcher schedules unsafe shortcut betting by default — YES (harness_pass.bat strategy line commented; supervisor clean).
6. every direct wallet.open_position is controlled or test-only — YES (repo scan test).
7. blocked shortcut attempts are visible with no-bet reasons — YES (print + obs + journal in safe_bet).
8. tests prove shortcut paths cannot place paper bets — YES (14 cases).
9. existing tests still pass — YES (57/57).
10. report written — YES (this file).
